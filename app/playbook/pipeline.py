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
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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


def _playbook_data_to_market_data(symbol: str, data, regime_ctx) -> Dict[str, Any]:
    """把 PlaybookData 转成 VotingCoordinator.run_voting() 期望的 market_data dict.
    
    VotingCoordinator 读取的关键字段 (model_agent.py + voting_coordinator.py):
      symbol, price, rsi, macd_signal, ma20, ma50, volume_ratio, vix, regime
    """
    tech = data.technical
    return {
        "symbol": symbol,
        "ticker": symbol,
        "price": tech.price or 0,
        "rsi": tech.rsi_14 or 50,
        "macd_signal": tech.macd_signal or "neutral",
        "ma20": tech.ma20 or 0,
        "ma50": tech.ma50 or 0,
        "volume_ratio": tech.volume_5d_avg_ratio or 1.0,
        "vix": getattr(regime_ctx, "vix", 20),
        "regime": regime_ctx.regime.value if hasattr(regime_ctx.regime, "value") else str(regime_ctx.regime),
    }


def _build_instructions_from_votes(
    screen_results,
    voting_coordinator,
    regime_context,
    total_equity: float,
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
    regime_str = str(regime_context.regime) if hasattr(regime_context, "regime") else "CAUTIOUS"
    
    # Regime 仓位乘数 (与 InstructionGenerator.REGIME_MULTIPLIERS 对齐)
    regime_mults = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}
    regime_mult = regime_mults.get(regime_str, 0.5)
    
    for sr in screen_results:
        symbol = sr.symbol
        data = sr.data
        factors = sr.factors
        
        # 构造 market_data 并调 VotingCoordinator
        market_data = _playbook_data_to_market_data(symbol, data, regime_context)
        
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
        )
        
        if not values or not values.get("entry_price"):
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="价格缺失, 无法计算数值"
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
        )
        instructions.append(inst)
        logger.info(
            f"[playbook] {symbol} vote→instruction: {vote_direction}→{direction} "
            f"strength={vote_result.get('final_strength', '?')} "
            f"consensus={vote_result.get('consensus_level', '?')} "
            f"decision_id={decision_id}"
        )
    
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
        instructions = _build_instructions_from_votes(
            screen_results=screen_results,
            voting_coordinator=voting_coordinator,
            regime_context=regime_ctx,
            total_equity=_get_total_equity(scheduler),
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
    }


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
                # 检查是否已追踪：所有指令均有终态 outcome (win/loss/skipped/missed)
                # outcome="holding"/NULL 均不算终态 — 持仓中或未追踪的指令需每日重追踪直到产生最终 outcome
                tracked = bool(instructions) and all(
                    inst.get("outcome") in ("win", "loss", "skipped", "missed")
                    for inst in instructions
                )
                if tracked:
                    logger.info(f"[playbook] {today} DB 已有追踪记录，跳过 (幂等)")
                    scheduler._playbook_tracked_date = today
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
        # OKR (2026-06-23): EOD 强制平仓兜底 — monitor_playbook_shadow_exits 的 PRE_CLOSE_EXIT
        # 只在 15:52-16:00 ET 的 8 分钟窗口内、且实时报价可用时才触发; 一旦该窗口错过 (报价
        # 失败/时序错位), 仓位永久孤儿化 (exit_price=0), 永不产生 win/loss → OKR 样本永不增长.
        # 这里在盘后追踪前, 对所有仍未平仓的 PLAYBOOK_SHADOW 仓位用当前价 (≈收盘价) 强制平仓,
        # 保证每个执行仓位当天必出最终 outcome. force=True 历史回补时也适用 (按当前价实现已开仓位).
        try:
            closed = _force_close_orphaned_shadow_positions(scheduler)
            if closed:
                logger.info(f"[playbook] EOD 强制平仓: {closed} 个孤儿 shadow 仓位已兜底平仓")
        except Exception as _e:
            logger.warning(f"[playbook] EOD 强制平仓异常 (已隔离, 不阻断追踪): {_e}")

        # Phase 1 (2026-06-24): futu_simulate 真实模拟仓位 EOD 兜底 (broker 平仓)
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
            # confidence=0 是 SKIP 指令 (已在上面过滤), 不会到这里
            _inst_conf = inst.get("confidence", 0) or 0
            if _inst_conf < 70:
                logger.info(f"[playbook] {inst['symbol']} confidence={_inst_conf} < 70, 跳过")
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
    """盘中实时检查指令是否触发 (价格进入入场区间 ±5%).

    与 shadow_tracker._track_instruction 的触发逻辑对齐:
        entry_low = entry_price * 0.95
        entry_high = entry_price * 1.05
        triggered = entry_low <= day_high and day_low <= entry_high

    盘中用当前实时价格替代 day_high/day_low 做快速判断.
    """
    entry_price = inst.get("entry_price")
    if not entry_price or entry_price <= 0:
        return False

    symbol = inst.get("symbol", "")
    if not symbol:
        return False

    # 获取当前价格
    current_price = None
    data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    if data_router:
        try:
            quote = data_router.get_quote(symbol)
            if quote and isinstance(quote, dict):
                current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
            elif hasattr(quote, "last"):
                current_price = float(quote.last)
        except Exception:
            pass

    if not current_price or current_price <= 0:
        return False

    entry_low = entry_price * 0.95
    entry_high = entry_price * 1.05
    triggered = entry_low <= current_price <= entry_high

    # 入场前止损检查: 如果当前价格已越过 stop_loss, 不入场 (避免秒退)
    if triggered:
        stop_loss = float(inst.get("stop_loss", 0) or 0)
        if stop_loss > 0:
            direction = inst.get("direction", "")
            if "SHORT" in direction.upper() and current_price >= stop_loss:
                logger.warning(
                    f"[playbook] ⚔️ 入场拦截: {symbol} current=${current_price:.2f} "
                    f"已越过 SHORT stop_loss=${stop_loss:.2f}, 跳过 (避免入场即止损)"
                )
                return False
            if "LONG" in direction.upper() and "SHORT" not in direction.upper() and current_price <= stop_loss:
                logger.warning(
                    f"[playbook] ⚔️ 入场拦截: {symbol} current=${current_price:.2f} "
                    f"已越过 LONG stop_loss=${stop_loss:.2f}, 跳过 (避免入场即止损)"
                )
                return False

    if triggered:
        logger.info(
            f"[playbook] 盘中触发检查: {symbol} price=${current_price:.2f} "
            f"in [{entry_low:.2f}, {entry_high:.2f}] (entry=${entry_price:.2f})"
        )

    return triggered


def monitor_playbook_shadow_exits(scheduler) -> int:
    """盘中监控 Playbook shadow 仓位的退出 — 镜像 live ExitMonitor v3.0 的4条规则.

    每60秒由 scheduler 在 TRADING 阶段调用.
    退出规则 (与 exit_policy.yaml v3.0 完全对齐):
      [1] ATR止损: entry ± 2.0×ATR, max 2.0% 硬上限 (SL_ATR_STOP)
      [2] 保本ratchet: peak_profit≥1.5% → SL移至entry (不平仓, 只保护)
      [3] 时间退出: 持仓>2h 且 盈利<-0.5% → 退出 (TIME_EXIT, 2026-06-24放宽)
      [4] 盘末退出: 收盘前8min → 全平 (PRE_CLOSE_EXIT)

    Returns: 本轮退出数量
    """
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime, timezone, timedelta

        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # 查找所有未退出的 playbook shadow 仓位
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, signal_id,
                   metadata_json, signal_details, mode, market_regime,
                   signal_strength, signal_confidence, entry_time
            FROM trade_records
            WHERE source_type = 'PLAYBOOK_SHADOW'
              AND (exit_price = 0 OR exit_price IS NULL)
              AND exit_reason = ''
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            conn.close()
            return 0

        # 可观测性: 确认 monitor 在跑 + 看到多少未平仓仓位 (否则无法判断平仓回填是否工作)
        logger.info(f"[playbook shadow] 退出监控: {len(rows)} 个未平仓 shadow 仓位待检查")

        exited = 0
        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)

        # ── 市场时段计算 (ET) ──
        ET = timezone(timedelta(hours=-4))
        now_et = datetime.now(ET)
        market_close_minutes = 16 * 60  # 16:00 ET
        now_minutes = now_et.hour * 60 + now_et.minute
        minutes_to_close = market_close_minutes - now_minutes
        # 美东时间 9:30 开盘
        market_open_minutes = 9 * 60 + 30
        minutes_from_open = now_minutes - market_open_minutes

        for r in rows:
            symbol = r["symbol"]
            direction = r["direction"]
            entry_price = float(r["entry_price"] or 0)
            if entry_price <= 0:
                continue

            # 从 metadata_json 提取 stop_loss / take_profit / ATR
            meta = {}
            try:
                meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                pass
            initial_sl = float(meta.get("stop_loss", 0))
            take_profit = float(meta.get("take_profit", 0))
            inst_id = meta.get("playbook_instruction_id")
            atr_value = float(meta.get("atr", 0))

            # 获取当前价格
            current_price = None
            if data_router:
                try:
                    quote = data_router.get_quote(symbol)
                    if quote and isinstance(quote, dict):
                        current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
                    elif hasattr(quote, "last"):
                        current_price = float(quote.last)
                except Exception:
                    pass

            if not current_price or current_price <= 0:
                logger.debug(f"[playbook shadow] {symbol} 无实时报价, 跳过本轮退出检查")
                continue

            # Price sanity check: exit_price=95.0 bug (6/23) — 拒绝偏离entry>50%的quote
            if entry_price > 0:
                deviation = abs(current_price - entry_price) / entry_price
                if deviation > 0.5:
                    logger.warning(
                        f"[playbook shadow] 价格异常: {symbol} entry={entry_price} "
                        f"quote={current_price} deviation={deviation:.1%}, 跳过"
                    )
                    continue

            # ── 盈利百分比计算 ──
            if direction == "LONG":
                profit_pct = (current_price - entry_price) / entry_price * 100
            else:
                profit_pct = (entry_price - current_price) / entry_price * 100

            # ── peak tracking (从 metadata 恢复, 更新后写回) ──
            peak_price = float(meta.get("peak_price", 0))
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

            # ── effective SL (考虑 breakeven ratchet) ──
            # 规则2: peak_profit ≥ 1.5% → SL移至entry
            effective_sl = initial_sl
            breakeven_triggered = peak_profit_pct >= 1.5
            if breakeven_triggered:
                if direction == "LONG":
                    effective_sl = max(effective_sl, entry_price)  # SL不低于entry
                else:
                    effective_sl = min(effective_sl, entry_price) if effective_sl > 0 else entry_price

            # ── 退出判断 (按优先级, if/elif/elif 链) ──
            # 优先级: ATR止损 > 盘末退出 > 时间退出
            # 止损必须先于时间退出检查 — 否则亏损仓位会被 TIME_EXIT 截断而永不触发止损
            exit_reason = ""
            exit_price = current_price

            # 持仓时间 (从 entry_time 计算, 不用 minutes_from_open)
            _held_minutes = 0
            _entry_time_str = r["entry_time"]
            if _entry_time_str:
                try:
                    _entry_dt = datetime.fromisoformat(_entry_time_str)
                    _held_minutes = (datetime.now() - _entry_dt).total_seconds() / 60
                except Exception:
                    pass

            # 规则1: ATR止损 (最高优先级 — 亏损仓位必须先检查止损)
            if effective_sl > 0:
                if direction == "LONG" and current_price <= effective_sl:
                    exit_reason = "SL_ATR_STOP"
                    exit_price = effective_sl
                elif direction == "SHORT" and current_price >= effective_sl:
                    exit_reason = "SL_ATR_STOP"
                    exit_price = effective_sl

            # 规则4: 盘末退出 (收盘前8min)
            # RFC-061 A/B: cohort B 盈利仓跳过 EOD 强平, 持有过夜到 ~T+5 (capture 周级 alpha)
            _cohort = meta.get("cohort", "A")
            if not exit_reason and 0 <= minutes_to_close <= 8 and minutes_from_open > 0:
                if _cohort == "B" and profit_pct > 0:
                    logger.info(
                        f"[playbook shadow] {symbol} cohort=B profit={profit_pct:.2f}% → 跳过 PRE_CLOSE 持有过夜 (RFC-061)"
                    )
                else:
                    exit_reason = "PRE_CLOSE_EXIT"
                    exit_price = current_price

            # 规则3: 时间退出 (2026-06-24: 1h/0.3%→2h/-0.5% — 让趋势发展, 避免过早砍仓)
            # 数据依据: 6/23 shadow 10笔 TIME_EXIT 净-$72, 其中6笔微利被砍后 EOD 本可+3.62%
            if not exit_reason and _held_minutes > 120 and profit_pct < -0.5:
                exit_reason = "TIME_EXIT"
                exit_price = current_price  # 市价退出

            # 规则5 (RFC-061): cohort B 硬上限 — 持仓 ≥7 日历日 (≈5 交易日, IC T+5 峰值后) 强制了结
            # 仅对 cohort B (过夜组) 生效; A 组日内已平, 不会触及
            if not exit_reason and _cohort == "B" and _held_minutes > 0:
                _days_held = _held_minutes / 60 / 24
                if _days_held >= 7:
                    exit_reason = "MAX_HOLD_T5"
                    exit_price = current_price
                    logger.info(
                        f"[playbook shadow] {symbol} cohort=B days_held={_days_held:.1f} → MAX_HOLD_T5 强平 (RFC-061)"
                    )


            if not exit_reason:
                # 更新 peak_price 到 metadata (即使不退出也要跟踪)
                if peak_price != float(meta.get("peak_price", 0)):
                    meta["peak_price"] = peak_price
                    meta["breakeven_triggered"] = breakeven_triggered
                    conn.execute(
                        "UPDATE trade_records SET metadata_json = ? WHERE id = ?",
                        (_json.dumps(meta), r["id"])
                    )
                    conn.commit()
                continue

            # ── 计算 PnL ──
            qty = r["quantity"]
            if direction == "LONG":
                pnl = (exit_price - entry_price) * qty
                pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
            else:
                pnl = (entry_price - exit_price) * qty
                pnl_pct = (entry_price - exit_price) / entry_price * 100 if entry_price else 0

            # 持仓时间
            entry_time_str = r["entry_time"]
            holding_hours = 0
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    holding_hours = (datetime.now() - entry_dt).total_seconds() / 3600
                except Exception:
                    pass

            now_iso = datetime.now().isoformat()

            # 1. 更新 trade_records
            conn.execute("""
                UPDATE trade_records
                SET exit_price = ?, exit_time = ?, exit_reason = ?,
                    realized_pnl = ?, pnl_pct = ?, holding_hours = ?
                WHERE id = ?
            """, (exit_price, now_iso, exit_reason, pnl, pnl_pct / 100,
                  holding_hours, r["id"]))
            conn.commit()

            # 2. 写 signal_lifecycle
            try:
                conn.execute("""
                    INSERT INTO signal_lifecycle (
                        trade_id, symbol, signal_strength, signal_confidence,
                        signal_direction, trade_direction, entry_price, exit_price,
                        realized_pnl, pnl_pct, exit_reason, holding_hours,
                        owner_strategy, source_type, trade_date, trade_mode, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["id"], symbol,
                    r["signal_strength"] or 0, r["signal_confidence"] or 0,
                    direction, direction, entry_price, exit_price,
                    pnl, pnl_pct / 100, exit_reason, holding_hours,
                    "playbook_shadow", "PLAYBOOK_SHADOW",
                    now_iso[:10], "simulate", now_iso,
                ))
                conn.commit()
            except Exception as _sl_e:
                logger.debug(f"[playbook shadow] signal_lifecycle 写入失败: {_sl_e}")

            # 3. 更新 playbook DB
            if inst_id:
                try:
                    from app.playbook.db import PlaybookDB
                    pb_db = PlaybookDB()
                    pb_db.update_instruction_tracking(inst_id, {
                        "outcome": "win" if pnl > 0 else "loss",
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "exit_time": now_iso,
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "holding_period_hours": holding_hours,
                    })
                except Exception:
                    pass

            exited += 1
            logger.info(
                f"[playbook shadow] 退出: {symbol} {direction} "
                f"reason={exit_reason} entry={entry_price} exit={exit_price} "
                f"pnl=${pnl:.2f} ({pnl_pct:+.1f}%) "
                f"peak={peak_price:.2f} be={'Y' if breakeven_triggered else 'N'}"
            )

        conn.close()
        return exited

    except Exception as e:
        logger.debug(f"[playbook shadow] exit monitor 异常 (已隔离): {e}")
        return 0


def _force_close_orphaned_shadow_positions(scheduler) -> int:
    """EOD 兜底: 强制平仓所有仍未平仓的 PLAYBOOK_SHADOW 仓位.

    OKR (2026-06-23): monitor_playbook_shadow_exits 的 PRE_CLOSE_EXIT 只在收盘前 8 分钟窗口
    内且报价可用时触发. 一旦错过窗口, 仓位 exit_price 永久为 0, outcome 卡在 'holding',
    永不产生 win/loss → OKR 样本无法增长, IC 无法计算. 本函数在盘后追踪前兜底: 用当前报价
    (盘后 ≈ 收盘价) 平掉所有孤儿仓位, 保证每个执行仓位当天必出最终 outcome.

    Returns: 本轮强制平仓数量
    """
    import sqlite3
    import json as _json
    from app.core.paths import get_trade_records_db
    from datetime import datetime

    db_path = str(get_trade_records_db())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time
            FROM trade_records
            WHERE source_type = 'PLAYBOOK_SHADOW'
              AND (exit_price = 0 OR exit_price IS NULL)
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            return 0

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook] EOD 兜底跳过: 无 data_router, 无法获取平仓价")
            return 0

        closed = 0
        now_iso = datetime.now().isoformat()

        for r in rows:
            symbol = r["symbol"]
            direction = r["direction"]
            entry_price = float(r["entry_price"] or 0)
            if entry_price <= 0:
                continue

            # 获取当前报价 (盘后 ≈ 收盘价)
            current_price = None
            try:
                quote = data_router.get_quote(symbol)
                if quote and isinstance(quote, dict):
                    current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
                elif hasattr(quote, "last"):
                    current_price = float(quote.last)
            except Exception as _e:
                logger.debug(f"[playbook] EOD 兜底 {symbol} 取价失败: {_e}")

            if not current_price or current_price <= 0:
                logger.warning(f"[playbook] EOD 兜底 {symbol} 无报价, 跳过 (仓位仍孤儿化)")
                continue

            # 价格健全性: 拒绝偏离 entry>50% 的异常报价
            if abs(current_price - entry_price) / entry_price > 0.5:
                logger.warning(
                    f"[playbook] EOD 兜底 {symbol} 价格异常 entry={entry_price} "
                    f"quote={current_price}, 跳过"
                )
                continue

            qty = r["quantity"] or 0
            if direction == "LONG":
                pnl = (current_price - entry_price) * qty
                pnl_pct = (current_price - entry_price) / entry_price * 100
            else:
                pnl = (entry_price - current_price) * qty
                pnl_pct = (entry_price - current_price) / entry_price * 100

            holding_hours = 0.0
            entry_time_str = r["entry_time"]
            if entry_time_str:
                try:
                    holding_hours = (datetime.now() - datetime.fromisoformat(entry_time_str)).total_seconds() / 3600
                except Exception:
                    pass

            # RFC-061 A/B: cohort B 盈利仓跳过 EOD 兜底强平, 持有过夜.
            # 但 cohort B 持仓 ≥7 日历日 (MAX_HOLD_T5) 仍须强平 (硬上限).
            # cohort B 亏损仓不跳过 (维持安全兜底). cohort A 维持原行为.
            try:
                _meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                _meta = {}
            _cohort = _meta.get("cohort", "A")
            _days_held = holding_hours / 24
            if _cohort == "B" and pnl > 0 and _days_held < 7:
                logger.info(
                    f"[playbook] EOD 兜底跳过 {symbol} cohort=B profit={pnl_pct:.2f}% 持有过夜 (RFC-061)"
                )
                continue
            _eod_reason = "MAX_HOLD_T5" if (_cohort == "B" and _days_held >= 7) else "EOD_FORCE_CLOSE"

            conn.execute("""
                UPDATE trade_records
                SET exit_price = ?, exit_time = ?, exit_reason = ?,
                    realized_pnl = ?, pnl_pct = ?, holding_hours = ?
                WHERE id = ?
            """, (current_price, now_iso, _eod_reason, pnl, pnl_pct / 100,
                  holding_hours, r["id"]))

            # 同步更新对应 playbook_instructions 的 outcome (双路径一致性).
            # 注意: trade_records 与 playbook_instructions 同在 data/ftseek_us.db, 复用同一 conn
            # 避免开第二个连接与未关闭的 trade_records 连接抢 SQLite 锁 (曾导致静默 update 失败).
            try:
                meta = _meta  # 已在上方解析, 复用
                inst_id = meta.get("playbook_instruction_id")
                if inst_id:
                    # 仅覆盖非终态 outcome (holding/NULL), 已是 win/loss 的不覆盖 (防止回退已终态仓位)
                    conn.execute(
                        "UPDATE playbook_instructions SET outcome=?, exit_price=?, "
                        "exit_time=?, exit_reason=?, pnl=?, pnl_pct=?, holding_period_hours=? "
                        "WHERE id=? AND outcome NOT IN ('win','loss')",
                        ("win" if pnl > 0 else "loss", current_price, now_iso,
                         _eod_reason, round(pnl, 2), round(pnl_pct, 2),
                         round(holding_hours, 1), inst_id),
                    )
            except Exception as _e:
                logger.debug(f"[playbook] EOD 兜底同步 instruction 失败 {symbol}: {_e}")

            conn.commit()
            closed += 1
            logger.info(
                f"[playbook] EOD 兜底平仓: {symbol} {direction} "
                f"entry={entry_price} exit={current_price} pnl=${pnl:.2f} ({pnl_pct:+.1f}%)"
            )

        return closed

    finally:
        conn.close()


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
              AND (exit_price = 0 OR exit_price IS NULL)
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
            if abs(current_price - entry_price) / entry_price > 0.5:
                continue  # 价格异常跳过

            if direction == "LONG":
                profit_pct = (current_price - entry_price) / entry_price * 100
            else:
                profit_pct = (entry_price - current_price) / entry_price * 100

            # 持仓时长
            held_minutes = 0
            try:
                held_minutes = (datetime.now() - datetime.fromisoformat(r["entry_time"])).total_seconds() / 60
            except Exception:
                pass

            # 退出判断
            exit_reason = ""
            if 0 <= minutes_to_close <= 8 and minutes_from_open > 0:
                exit_reason = "PRE_CLOSE_EXIT"
            elif held_minutes > 120 and profit_pct < -0.5:  # 2026-06-24: 1h/0.3%→2h/-0.5%
                exit_reason = "TIME_EXIT"
            elif stop_loss > 0:
                if direction == "LONG" and current_price <= stop_loss:
                    exit_reason = "SL_ATR_STOP"
                elif direction == "SHORT" and current_price >= stop_loss:
                    exit_reason = "SL_ATR_STOP"

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
                logger.warning(f"[playbook dual] 平仓失败 {symbol} ({_row_target}): {close_res.get('error')}")
                continue

            exit_price = close_res.get("filled_price", 0) or current_price
            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            now_iso = datetime.now().isoformat()
            holding_hours = round(held_minutes / 60, 1)

            conn.execute("""
                UPDATE trade_records
                SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?
                WHERE id=?
            """, (exit_price, now_iso, exit_reason, pnl, pnl_pct / 100, holding_hours, r["id"]))
            # 同步 instruction outcome
            if inst_id:
                conn.execute(
                    "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, "
                    "holding_period_hours=? WHERE id=? AND outcome NOT IN ('win','loss')",
                    ("win" if pnl > 0 else "loss", exit_price, exit_reason, round(pnl, 2), round(pnl_pct, 2),
                     holding_hours, inst_id),
                )
            conn.commit()
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
              AND (exit_price = 0 OR exit_price IS NULL)
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

            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            try:
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
            except Exception as _e:
                logger.warning(f"[playbook dual] EOD 兜底平仓异常 {symbol} ({_row_target}): {_e}")
                continue
            if not close_res.get("success"):
                logger.warning(f"[playbook dual] EOD 兜底 {symbol} ({_row_target}) 无报价/平仓失败: {close_res.get('error')}")
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

            conn.execute("""
                UPDATE trade_records SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?
                WHERE id=?
            """, (exit_price, now_iso, "EOD_FORCE_CLOSE", pnl, pnl_pct / 100, holding_hours, r["id"]))
            try:
                meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                inst_id = meta.get("playbook_instruction_id")
                if inst_id:
                    conn.execute(
                        "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, "
                        "holding_period_hours=? WHERE id=? AND outcome NOT IN ('win','loss')",
                        ("win" if pnl > 0 else "loss", exit_price, "EOD_FORCE_CLOSE", round(pnl, 2), round(pnl_pct, 2),
                         round(holding_hours, 1), inst_id),
                    )
            except Exception as _e:
                logger.debug(f"[playbook sim] EOD 同步 instruction 失败 {symbol}: {_e}")
            conn.commit()
            closed += 1
            logger.info(f"[playbook sim] EOD 兜底平仓: {symbol} exit={exit_price} pnl=${pnl:.2f}")

        conn.close()
        return closed

    except Exception as e:
        logger.warning(f"[playbook sim] EOD force_close 异常 (已隔离): {e}")
        return 0

