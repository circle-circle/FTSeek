"""
Playbook 端到端编排管线 — 把 Layer 0→1→2→3 串联通电。

本模块是整个 Trading Playbook System 唯一缺失的"接线层":
    - Phase 0/1/2 的 14 个模块都已实现且单测通过 (232 tests),
      但没有任何生产代码调用它们 — scheduler 不感知 playbook 的存在。
    - 本文件提供 3 个入口, 复用项目现有调度约定 (phase-polling + 每日/每周幂等标志):
        run_playbook_generate(scheduler)   盘前  regime→收集→筛选→批量LLM→入库→飞书
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


# ── LLM 适配器 ─────────────────────────────────────────────────────────
# InstructionGenerator 期望 llm_client.call(feature=, prompt=, max_tokens=, temperature=)
# 返回原始字符串; 而项目统一入口是 call_llm(feature, prompt) -> CallResult。
# 这里包一层适配器, 让 InstructionGenerator 无需改动。
class _CallLLMAdapter:
    """把 app.llm.model_call.call_llm 适配成 InstructionGenerator 期望的 .call 接口"""

    def call(self, feature: str, prompt: str, max_tokens: int = 2048, temperature: float = 0) -> str:
        from app.llm.model_call import call_llm
        res = call_llm(feature, prompt, max_tokens=max_tokens, temperature=temperature, json_mode=True)
        if not res or not getattr(res, "success", False):
            raise RuntimeError(f"LLM 调用失败 feature={feature}: {getattr(res, 'error', 'unknown')}")
        resp = res.response
        if isinstance(resp, (dict, list)):
            resp = json.dumps(resp, ensure_ascii=False)
        return str(resp)


# ── 依赖装配 (lazy, 单步异常隔离) ──────────────────────────────────────
def _build_data_collector():
    """构造 PlaybookDataCollector, 复用项目既有 Provider"""
    from app.playbook.data_collector import PlaybookDataCollector
    from app.data.providers.fundamental_provider import FundamentalProvider
    from app.data.finnhub_provider import FinnhubProvider
    from app.data.sec_form4_provider import SECForm4Provider
    from app.data.earnings_calendar import EarningsCalendar

    return PlaybookDataCollector(
        fundamental_provider=FundamentalProvider(),
        finnhub_provider=FinnhubProvider(),
        insider_provider=SECForm4Provider(),
        earnings_calendar=EarningsCalendar(),
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


# ── 入口 1: 盘前生成 (regime → 收集 → 筛选 → 批量 LLM → 入库 → 飞书) ──
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
        from app.playbook.instruction_generator import InstructionGenerator
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

        # Layer 1: 数据收集 + 规则筛选
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
            top_n=regime_ctx.top_n,
        )
        logger.info(f"[playbook] Layer1 筛选出 {len(screen_results)} 只候选")

        # Layer 1: 批量 LLM 生成指令 (反幻觉架构: LLM 只决策, Python 算数值)
        candidates = [
            {"symbol": r.symbol, "score": r.score, "factors": r.factors, "data": r.data}
            for r in screen_results
        ]

        # 注入历史 Pattern (spec 6.5) — 冷启动期为空, 无影响; 单独隔离避免 schema 异常阻断生成
        try:
            active_patterns = db.get_patterns(status="active") or []
            retired_patterns = db.get_patterns(status="retired") or []
        except Exception as e:
            logger.debug(f"[playbook] pattern 注入失败 (按冷启动处理): {e}")
            active_patterns, retired_patterns = [], []

        generator = InstructionGenerator(llm_client=_CallLLMAdapter(), risk_pct=1.0)
        instructions = generator.generate(
            candidates=candidates,
            regime_context=regime_ctx,
            positions=positions,
            total_equity=_get_total_equity(scheduler),
            active_patterns=active_patterns,
            retired_patterns=retired_patterns,
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
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                instructions = db.get_instructions_by_date(today)
                # 检查是否已追踪：任意指令有 shadow_pnl 或 outcome
                tracked = any(
                    inst.get("shadow_pnl") is not None or inst.get("outcome")
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
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                regime_log = db.get_regime_log(days=7)
                # 检查本周是否已出周报: regime_log 中有 weekly_report 类型记录
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
                if getattr(scheduler, "_playbook_weekly_done", None) == week_end:
                    logger.debug(f"[playbook] 本周 ({week_end}) 已出周报 (内存标志), 跳过")
                    return None

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
        return report

    except Exception as e:
        logger.exception(f"[playbook] 周度复盘异常 (已隔离): {e}")
        return {"error": str(e)}


# ── 入口 4: 盘中自动执行 Paper Trade (Phase 1: 2026-06-19) ──────────
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
            # 跳过已执行、SKIP 方向、未触发的指令
            if inst.get("executed"):
                continue
            if inst.get("direction") == "SKIP":
                continue
            if not inst.get("triggered"):
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
