"""
信号管线 — FTSeek-US 核心决策链路

从 scheduler_tasks.py 拆分而来。本模块包含完整的信号生成到决策管线，
回测适配层和实盘调度器共用此代码。

管线流程:
    scan_loop()                  # 主循环: 市场门禁 + 熔断 + 巡检
      └─ _run_signal_scan_impl() # 自适应轮询批次
           └─ _process_symbol_impl()  # 单标的完整决策链路
                ├─ 行情获取 + 动态成本注入
                ├─ 财报感知 + VIX Regime 注入
                ├─ 作战计划快车道 (跳过投票)
                ├─ 信号生成 (盘中技术面 / 盘后LLM投票)
                ├─ 大盘联动过滤 + 金字塔加仓检测
                ├─ 共识检查 + SignalScore 构建
                └─ 风控检查 → 下单委托
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from app.utils.timezone import now_et


def _get_vwap_deviation(scheduler, symbol: str) -> Optional[float]:
    """P1 VWAP Gate helper: 从 market_data 缓存获取 VWAP 偏离度。
    返回 None 表示数据不可用 (pre_trade_check 将软放行)。
    """
    try:
        if scheduler and hasattr(scheduler, 'market_data') and scheduler.market_data:
            ctx = scheduler.market_data.get_full_market_context(symbol)
            if ctx and "ticker_vwap_deviation" in ctx:
                return float(ctx["ticker_vwap_deviation"])
    except Exception:
        pass
    return None

from app.core.models import (
    SignalScore, Direction, MarketRegime,
)
from app.storage.rejection_logger import get_rejection_logger

logger = logging.getLogger(__name__)


# ============================================================
#  主循环: scan_loop
# ============================================================
def scan_loop(scheduler):
    """
    Phase 4 升级：信号扫描主循环 (带巡检与熔断)

    P3 优化 2026-04-29: 时间感知扫描 — 根据美股日内时段调整扫描频率
    - 开盘驱动 (09:30-10:30 ET): 3min 高频
    - 午餐低量 (12:00-14:00 ET): 暂停扫描
    - 尾盘驱动 (14:00-15:30 ET): 5min 中频
    - 其他时段: 10min 维持

    Args:
        scheduler: TradingScheduler 实例
    """
    from app.harness.us_audit import (
        is_circuit_breaker_open,
        record_api_success,
        record_api_error,
        run_pre_market_audit,
    )

    if not hasattr(scheduler, 'harness_audit_done'):
        scheduler.harness_audit_done = False

    _earnings_refresh_counter = 0
    _EARNINGS_REFRESH_INTERVAL = 10

    while scheduler.running:
        try:
            # P2-8 (2026-05-14): watchdog 心跳
            try:
                from app.observability.watchdog import get_watchdog
                get_watchdog().ping("scan_loop")
            except Exception:
                pass
            from app.core.scheduler_triggers import get_market_phase, MarketPhase
            phase = get_market_phase()
            if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                logger.info(f"[扫描] {phase.value} → 完全静默，跳过扫描")
                for _ in range(int(scheduler.SCAN_INTERVAL_MINUTES * 60)):
                    if not scheduler.running:
                        return
                    time.sleep(1)
                continue

            if is_circuit_breaker_open():
                logger.warning("[熔断] API 熔断器已开启，暂停扫描...")
                for _ in range(int(scheduler.SCAN_INTERVAL_MINUTES * 60)):
                    if not scheduler.running:
                        return
                    time.sleep(1)
                continue

            # P0-fix 2026-05-02: Kill Switch 检查
            from app.us_pipeline.emergency_interceptor import check_kill_switch
            if check_kill_switch():
                logger.critical("🚨 KILL SWITCH 激活 — 立即停止所有交易扫描")
                # 将风险状态设为 HALT
                try:
                    from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
                    rsm = RiskStateMachine()
                    if rsm.get_state() != RiskState.HALT:
                        rsm.set_state(RiskState.HALT, "Kill Switch activated", "EmergencyInterceptor")
                except Exception:
                    pass
                for _ in range(30):  # 每 30s 检查一次是否解除
                    if not scheduler.running:
                        return
                    from app.us_pipeline.emergency_interceptor import check_kill_switch as _cks
                    if not _cks():
                        logger.info("🟢 KILL SWITCH 已解除，恢复扫描")
                        break
                    time.sleep(1)
                continue

            if not scheduler.harness_audit_done:
                try:
                    audit_res = run_pre_market_audit(scheduler)
                    scheduler.harness_audit_done = True
                    if audit_res and not audit_res.get("passed", True):
                        logger.warning(f"[审计] 盘前审计未通过: {audit_res}")
                except Exception as _e:
                    logger.debug(f"[审计] 盘前审计失败: {_e}")
                    scheduler.harness_audit_done = True  # 不再重试

            # P0-fix 2026-05-02: 教训约束注入到 context — 每轮扫描加载活跃教训
            try:
                li = getattr(scheduler, 'lesson_injector', None)
                if li is not None:
                    scheduler._lesson_constraints = li.generate_trade_constraints()
            except Exception:
                pass
            if scheduler._is_market_open():
                scheduler.post_market_audit_done = False
                logger.info("[扫描] 开始信号扫描...")
                try:
                    _run_signal_scan_impl(scheduler)
                    record_api_success()
                except Exception as e:
                    record_api_error()
                    logger.error(f"[扫描] 扫描异常: {e}", exc_info=True)
            else:
                logger.debug(f"[扫描] {phase.value} → 跳过行情轮询")
                if not getattr(scheduler, 'post_market_audit_done', False):
                    try:
                        from app.harness.us_audit import run_post_market_audit
                        run_post_market_audit(scheduler)
                        scheduler.post_market_audit_done = True
                    except Exception as e:
                        logger.error(f"[巡检] 盘后巡检失败: {e}")

            scheduler._check_daily_review()
            scheduler._check_weekly_review()
            scheduler._check_monthly_review()
            scheduler._check_position_snapshot()

            _earnings_refresh_counter += 1
            if _earnings_refresh_counter >= _EARNINGS_REFRESH_INTERVAL:
                _earnings_refresh_counter = 0
                try:
                    earnings_cal = getattr(scheduler, 'earnings_calendar', None)
                    earnings_guard = getattr(scheduler, 'earnings_risk_guard', None)
                    if earnings_cal and earnings_guard:
                        earnings_list = earnings_cal.get_watchlist_earnings(days_ahead=7, force_refresh=True)
                        if earnings_list:
                            from app.data.earnings_calendar import get_sector_atr_multiplier
                            for entry in earnings_list:
                                sector = entry.get("sector", "Unknown")
                                atr_cfg = get_sector_atr_multiplier(sector, is_earnings_period=True)
                                entry["atr_multiplier"] = atr_cfg["earnings"]
                                entry["flash_exit_multiplier"] = atr_cfg["flash_exit"]
                            earnings_guard.update_earnings_calendar(earnings_list)
                            _log_fn = logger.info if scheduler._is_market_open() else logger.debug
                            _log_fn("[Earnings] 财报日历已刷新: %d 只股票 (未来7天)", len(earnings_list))
                except Exception as e:
                    logger.debug("[Earnings] 刷新失败 (不影响主流程): %s", e)

        except Exception as e:
            logger.error(f"[扫描] 循环异常：{e}", exc_info=True)

        _wait_interval = _get_scan_wait_seconds(scheduler)
        _wait_start = time.time()
        while scheduler.running and (time.time() - _wait_start) < _wait_interval:
            time.sleep(1)


def _get_scan_wait_seconds(scheduler) -> int:
    """
    P3 优化 (2026-05-05): 时间轮转协调器 - 根据时段动态调整主通道扫描频率。
    核心理念: 不在错误的时间用错误的策略。
    
    配合 Scalping 的活跃时段，实现策略轮转 (Time Rotation)。
    """
    try:
        import pytz
        ET = pytz.timezone("America/New_York")
        et_now = datetime.now(ET)
        hour = et_now.hour
        minute = et_now.minute
    except Exception:
        return scheduler.SCAN_INTERVAL_MINUTES * 60

    # 周末/假日
    if et_now.weekday() >= 5:
        return scheduler.SCAN_INTERVAL_MINUTES * 60

    # Feature flag: 检查是否启用自适应扫描
    try:
        from app.core.config_container import get_config as get_app_config
        cfg = get_app_config()
        adaptive = cfg.get_dotted("scheduler.adaptive_scan_enabled", True) # Default True for rotation
    except Exception:
        adaptive = True

    if not adaptive:
        # 回退到旧逻辑 (兼容)
        et_hour = hour + minute / 60.0
        if 9.5 <= et_hour < 10.5: return 3 * 60
        if 12.0 <= et_hour < 14.0: return 15 * 60
        if 14.0 <= et_hour < 15.5: return 5 * 60
        if 15.5 <= et_hour < 16.0: return 10 * 60
        return scheduler.SCAN_INTERVAL_MINUTES * 60

    # --- 时间轮转策略 (Time Rotation) ---
    # 依据: 市场微观结构 (Opening Drive, Morning, Midday, Afternoon, Closing)
    t = hour * 60 + minute  # 当前时间的分钟数 (ET)

    # 1. 开盘爆发期 (09:30-10:00): Scalping Primary, 主通道减少扫描 (防止噪音)
    if 570 <= t < 600:    
        return 10 * 60    # 10min (Reduced)

    # 2. 上午趋势期 (10:00-11:30): 双引擎活跃
    if 600 <= t < 690:    
        return 5 * 60     # 5min (Standard)

    # 3. 午盘量谷期 (11:30-14:00): Scalping Primary (均值回归), 主通道 Reduced
    if 690 <= t < 840:    
        return 12 * 60    # 12min (Reduced)

    # 4. 下午趋势期 (14:00-15:30): 趋势再起，双引擎活跃
    if 840 <= t < 930:    
        return 5 * 60     # 5min (Standard)

    # 5. 尾盘 MOC 窗口 (15:30-16:00): Scalping ONLY, 主通道禁止开新仓
    # 这里我们保持 60min 扫描以监控平仓逻辑，但不处理新信号 (信号阶段会拦截)
    if 930 <= t < 960:    
        return 60 * 60    # 60min (Off/Minimal)

    # 盘前/盘后: 使用默认间隔
    return scheduler.SCAN_INTERVAL_MINUTES * 60

# ============================================================
#  自适应信号扫描
# ============================================================
def _run_signal_scan_impl(scheduler):
    """自适应信号扫描实现 (2026-04-15 优化)"""
    from app.harness.us_audit import is_circuit_breaker_open, record_api_success, record_api_error

    spy_context = scheduler.market_data.get_spy_context()
    try:
        vix = scheduler.market_data.get_vix()
    except Exception:
        vix = 20.0
    market_news = scheduler.news.get_market_news_summary(hours=6)

    spy_change = spy_context.get("spx_change_percent", 0)
    if scheduler.polling_controller:
        scheduler.polling_controller.event_trigger.on_market_move(spy_change, vix)

    logger.info(
        f"[扫描] VIX={vix:.1f} SPY={spy_change:.2%} "
        f"市场情绪={market_news.get('sentiment', 0):.2f}"
    )

    # ── P4 2026-05-02: 动态选股池 ──
    # 根据 tier + 风控等级动态过滤 watchlist
    pool_filter = _get_dynamic_pool_filter(scheduler)

    # ── P0 修复 (2026-05-04): DEGRADED 模式下降级置信度/信号分阈值 ──
    _saved_min_confidence = scheduler.MIN_CONFIDENCE
    _saved_min_strength = getattr(scheduler, 'MIN_SIGNAL_STRENGTH', 50)
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        plan = USBattlePlanManager().get_plan()
        if plan.get("market_regime") == "DEGRADED":
            scheduler.MIN_CONFIDENCE = min(0.90, scheduler.MIN_CONFIDENCE + 0.05)
            scheduler.MIN_SIGNAL_STRENGTH = min(99, _saved_min_strength + 5)
            logger.info(
                "[降级模式] 阈值上调(更保守): 置信度 %.2f→%.2f | 信号分 %d→%d",
                _saved_min_confidence, scheduler.MIN_CONFIDENCE,
                _saved_min_strength, scheduler.MIN_SIGNAL_STRENGTH,
            )
    except Exception:
        pass

    try:
        _run_signal_scan_body(scheduler, vix, spy_context, market_news, pool_filter)
    finally:
        scheduler.MIN_CONFIDENCE = _saved_min_confidence
        scheduler.MIN_SIGNAL_STRENGTH = _saved_min_strength


def _run_signal_scan_body(scheduler, vix, spy_context, market_news, pool_filter):
    """_run_signal_scan_impl 的主体，提取出来以支持 try/finally 阈值恢复"""
    from app.harness.us_audit import is_circuit_breaker_open, record_api_success, record_api_error

    if scheduler.polling_controller:
        batch = scheduler.polling_controller.get_polling_batch()
        if not batch:
            logger.debug("[扫描] 本轮无到期股票，跳过")
            return

        # 动态池过滤 batch
        if pool_filter:
            original_len = len(batch)
            batch = _apply_pool_filter(scheduler, batch, pool_filter)
            if len(batch) < original_len:
                logger.info(
                    f"[动态池] 过滤后: {original_len} → {len(batch)} 只 "
                    f"(tier={pool_filter.get('tier')}, risk={pool_filter.get('risk_level')})"
                )

        logger.info(f"[自适应扫描] 本轮轮询 {len(batch)} 只股票：{batch}")

        for symbol in batch:
            if is_circuit_breaker_open():
                logger.warning("[熔断] 熔断器开启，终止扫描")
                break
            try:
                if scheduler._is_in_cooldown(symbol):
                    logger.debug(f"[扫描] {symbol} 处于冷却中，跳过")
                    continue
                if scheduler.position_store.has(symbol):
                    # C3 修复 (2026-05-13): 区分 scalping 持仓 vs 主管线持仓
                    existing = scheduler.position_store.get(symbol)
                    existing_source = (getattr(existing, 'metadata', None) or {}).get("source", "")
                    if existing_source == "scalping":
                        logger.debug(f"[扫描] {symbol} 由 Scalping 持有, 主管线仍进入金字塔评估")
                    else:
                        logger.debug(f"[扫描] {symbol} 已有主管线持仓, 进入金字塔加仓检测")
                _process_symbol_impl(scheduler, symbol, vix, spy_context, market_news.get("sentiment", 0))
                record_api_success()
            except Exception as e:
                record_api_error()
                logger.error(f"[扫描] {symbol} 处理异常：{e}", exc_info=True)
    else:
        if scheduler.position_store.count() >= scheduler.MAX_POSITIONS:
            logger.info(f"[扫描] 持仓已满({scheduler.position_store.count()}/{scheduler.MAX_POSITIONS})，跳过信号扫描")
            return

        # P2 2026-05-01: Collect-Rank-Allocate 漏斗
        # 收集所有通过门禁的信号候选, 统一评分排序后再分配风险预算执行
        candidates = []
        def _collect(symbol, signal, ctx, vr):
            candidates.append((symbol, signal, ctx, vr))

        for symbol in scheduler.watchlist:
            if is_circuit_breaker_open():
                logger.warning("[熔断] 熔断器开启，终止扫描")
                break
            # 动态池过滤
            if pool_filter and not _passes_pool_filter(scheduler, symbol, pool_filter):
                logger.debug(f"[动态池·过滤] {symbol} 不满足当前池条件 (tier={pool_filter.get('tier')}, risk={pool_filter.get('risk_level')})")
                continue
            try:
                if scheduler._is_in_cooldown(symbol):
                    continue
                if scheduler.position_store.has(symbol):
                    # C3 修复 (2026-05-13): 检查是否 scalping 持有
                    # 如果是 scalping 仓位, 主管线仍可评估 (由跨引擎锁和风险检查决定)
                    existing = scheduler.position_store.get(symbol)
                    existing_source = (getattr(existing, 'metadata', None) or {}).get("source", "")
                    existing_dir = getattr(existing, 'direction', None)
                    existing_dir_str = existing_dir.value if hasattr(existing_dir, 'value') else str(existing_dir)
                    if existing_source == "scalping":
                        logger.info(
                            f"[跨引擎] {symbol} 由 Scalping 持有({existing_dir_str}), "
                            f"主管线仍评估信号 (方向冲突由风险层裁决)"
                        )
                        # C5 归因 (2026-05-13): 记录互斥事件
                        try:
                            from app.analysis.strategy_attribution import get_attribution
                            get_attribution().record_interference(
                                symbol=symbol,
                                interference_type="main_blocked_by_scalping",
                                blocked_strategy="main",
                                blocked_direction="",  # 尚不知道主线的信号方向
                                detail={"scalping_direction": existing_dir_str},
                            )
                        except Exception:
                            pass
                        # 不 skip, 让主管线继续评估 — 方向冲突由后续风险检查处理
                    else:
                        continue
                # P1 (2026-05-26): 新管线 CRA 模式 — 用 modular stages 生成候选,
                # 然后走 PortfolioConstructor 全局排序, 风控统一在 _submit_order 执行
                if _use_new_pipeline():
                    _candidate = _run_new_pipeline_to_candidate(
                        scheduler, symbol, vix, spy_context, market_news.get("sentiment", 0))
                    if _candidate is not None:
                        _sig, _ctx, _vr = _bridge_candidate_to_old_format(_candidate)
                        _collect(symbol, _sig, _ctx, _vr)
                else:
                    _process_symbol_impl(scheduler, symbol, vix, spy_context,
                                         market_news.get("sentiment", 0),
                                         on_candidate=_collect)
                record_api_success()
            except Exception as e:
                record_api_error()
                logger.error(f"[扫描] {symbol} 处理异常：{e}", exc_info=True)

        # 组合构建: 评分 → 排序 → 分配 → 执行
        if candidates:
            _execute_portfolio_plan(scheduler, candidates, vix)
        else:
            logger.info("[组合构建] 本轮无合格候选")


# ============================================================
#  P1 (2026-05-26): CRA 模式下的新管线集成
# ============================================================

def _run_new_pipeline_to_candidate(scheduler, symbol, vix, spy_context, market_sentiment):
    """运行新管线 market_data + signal 阶段 (不含 risk), 返回 SignalCandidate

    CRA 模式专用: 只生成信号候选, 风控和仓位计算延迟到 _submit_order 统一执行。
    """
    from app.pipeline.stages import market_data_stage, signal_stage

    earnings_guard = getattr(scheduler, 'earnings_risk_guard', None)
    shared_kernel = None
    try:
        from app.core.shared_kernel import get_shared_kernel
        shared_kernel = get_shared_kernel()
    except Exception:
        pass

    battle_plan_manager = None
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        if mgr._plan:
            battle_plan_manager = mgr
    except Exception:
        pass

    # Stage 1: market_data
    ctx = market_data_stage.process(
        symbol=symbol,
        data_router=scheduler.market_data,
        vix=vix,
        spy_context=spy_context,
        earnings_guard=earnings_guard,
        shared_kernel=shared_kernel,
    )
    if ctx is None:
        return None

    # Stage 2: signal
    candidate = signal_stage.process(
        ctx=ctx,
        position_store=scheduler.position_store,
        battle_plan_manager=battle_plan_manager,
        voting_coordinator=getattr(scheduler, 'voting_coordinator', None),
    )
    return candidate


def _bridge_candidate_to_old_format(candidate):
    """将 SignalCandidate 桥接为旧格式 (signal, context, vote_result) 供 CRA 使用"""
    from app.core.models import SignalScore, Direction as SignalDirection
    import uuid
    from datetime import datetime

    symbol = candidate.symbol
    direction = SignalDirection.LONG if candidate.is_long else SignalDirection.SHORT

    signal = SignalScore(
        symbol=symbol,
        direction=direction,
        strength=candidate.strength,
        confidence=candidate.confidence,
        strategy_id=candidate.strategy_id,
        trace_id=getattr(candidate, "trace_id", "")
            or f"US-{now_et().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
        metadata={
            "source": candidate.source,
            "rationale": candidate.rationale,
            "factors_used": getattr(candidate, "signal_tags", []) or [],
            "signal_tags": "/".join(getattr(candidate, "signal_tags", []) or []) or candidate.strategy_id,
        },
    )

    ctx = candidate.context
    context = {}
    if ctx:
        context = {
            "symbol": symbol, "price": ctx.price,
            "bid": ctx.bid, "ask": ctx.ask,
            "lv3_bid": ctx.bid, "lv3_ask": ctx.ask,
            "prev_close": ctx.prev_close,
            "change_percent": ctx.change_pct * 100,
            "volume": ctx.volume, "avg_volume": ctx.avg_volume,
            "rsi": ctx.rsi, "macd": ctx.macd, "macd_signal": ctx.macd_signal,
            "bollinger_upper": ctx.bollinger_upper, "bollinger_lower": ctx.bollinger_lower,
            "supertrend_direction": ctx.supertrend_direction,
            "atr": ctx.atr, "atr_pct": ctx.atr_pct,
            "ma5": ctx.ma5, "ma20": ctx.ma20, "ma50": ctx.ma50, "ma200": ctx.ma200,
            "spread_bps": ctx.spread_bps, "adv_shares": ctx.adv_shares,
            "vix": ctx.vix, "vix_regime": ctx.vix_regime,
            "spy_price": ctx.spy_price, "spy_ma200": ctx.spy_ma200,
            "market_sentiment": ctx.market_sentiment,
            "news_sentiment": ctx.news_sentiment,
            "is_earnings_period": ctx.earnings_blackout,
            "is_earnings_blackout": ctx.earnings_blackout,
            "data_source": ctx.source,
        }

    vote_result = {
        "symbol": symbol,
        "direction": candidate.direction,
        "final_strength": candidate.strength,
        "consensus_level": candidate.confidence,
        "final_confidence": candidate.confidence,
        "std_dev": 0,
        "strategy_id": candidate.strategy_id,
        "position_suggestion": {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
    }

    return signal, context, vote_result


def run_signal_scan(scheduler):
    """遍历股票池，生成并执行交易信号 - 外部调用入口"""
    _run_signal_scan_impl(scheduler)


def process_symbol(scheduler, symbol: str, vix: float, spy_context: Dict[str, Any], market_sentiment: float):
    """处理单只股票：数据获取 → 信号生成 → 风控 → 下单"""
    _process_symbol_impl(scheduler, symbol, vix, spy_context, market_sentiment)


# ============================================================
#  技术面信号生成
# ============================================================
def _generate_technical_signal(scheduler, context: Dict[str, Any]) -> Dict[str, Any]:
    """盘中纯技术面信号生成 — IntradayScorer v2.0 多因子引擎"""
    from app.core.intraday_scorer import get_intraday_scorer

    scorer = get_intraday_scorer()
    signal = scorer.score(context)
    result = signal.to_vote_result()

    result["position_suggestion"].update({
        "stop_loss_pct": signal.stop_loss_pct,
        "atr_used": context.get("atr", 0),
    })
    result["market_regime"] = signal.flags.get("regime_label", "NORMAL")

    scheduler.voting_coordinator._record_decision_to_db(result, context, external_votes=None)
    
    # 【新增】影子测试数据记录
    try:
        from app.storage.daily_log_store import DailyLogStore
        log_store = DailyLogStore()
        log_store.record_tick({
            'time': context.get('time', context.get('timestamp', '')),
            'symbol': context.get('symbol', ''),
            'close': context.get('price', 0),
            'atr': context.get('atr', 0),
            'vix': context.get('vix', 0),
            'score': result.get('strength', 0),
            'direction': result.get('direction', ''),
            'is_entry': False, # Will be updated by execution layer
            'is_exit': False
        })
    except Exception as dle:
        logger.debug(f"[ShadowLog] 记录失败: {dle}")
        
    return result


# ============================================================
#  P2 2026-05-01: 组合构建执行 — 评分 → 排序 → 分配 → 下单
# ============================================================
def _execute_portfolio_plan(scheduler, candidates: list, vix: float):
    """将收集到的候选送入 PortfolioConstructor, 评分排序后分配执行"""
    from app.pipeline.portfolio_constructor import get_portfolio_constructor
    from app.core.shared_kernel import get_shared_kernel
    from app.risk.capital_safety import get_capital_safety_system

    pc = get_portfolio_constructor()
    pc.reset()

    # P1 G2: 加载作战计划用于方向匹配评分
    battle_plan_targets = {}
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        plan = mgr.get_plan()
        for t in plan.get("targets", []):
            battle_plan_targets[t.get("symbol", "")] = t.get("direction", "ANY")
    except Exception as e:
        logger.debug(f"[组合构建] 作战计划加载失败 (不影响评分): {e}")

    for symbol, signal, context, vote_result in candidates:
        plan_dir = battle_plan_targets.get(symbol, "")
        pc.add_candidate(symbol, signal, context, vote_result,
                         battle_plan_direction=plan_dir)

    # 获取评分所需上下文
    kernel = get_shared_kernel()
    current_session = kernel.get_current_session()
    vix_regime = kernel.get_vix_regime()
    capital = get_capital_safety_system()

    # 计算当前净敞口
    positions = scheduler.position_store.all()
    equity = capital._state.current_equity if capital._state.current_equity > 0 else 100000.0
    long_val = sum(p.size * p.avg_entry_price for p in positions if p.direction == Direction.LONG)
    short_val = sum(p.size * p.avg_entry_price for p in positions if p.direction == Direction.SHORT)
    net_exposure = (long_val - short_val) / equity if equity > 0 else 0.0
    total_exposure = (long_val + short_val) / equity if equity > 0 else 0.0

    logger.info(
        f"[组合构建] 候选={pc.candidate_count} | 当前持仓={len(positions)} | "
        f"net敞口={net_exposure:+.1%} | total敞口={total_exposure:.1%} | "
        f"session={current_session} | regime={vix_regime}"
    )

    # 评分 + 排序
    pc.score_candidates(current_session, vix_regime, net_exposure, total_exposure)

    # 风险预算分配
    execution_plan = pc.allocate_risk_budget(
        capital_safety=capital,
        max_positions=scheduler.MAX_POSITIONS,
        existing_positions=positions,
    )

    # 执行: 按排名依次下单 (P0: 跨引擎互斥锁保护开仓临界区)
    if not execution_plan:
        return

    from app.pipeline.order_executor import _submit_order
    lock = getattr(scheduler, '_open_position_lock', None)

    for c in execution_plan:
        try:
            if lock:
                lock.acquire()
            try:
                _submit_order(scheduler, c.symbol, c.signal, c.context, c.vote_result)
            finally:
                if lock:
                    lock.release()
        except Exception as e:
            logger.error(f"[组合执行] {c.symbol} 下单失败: {e}", exc_info=True)

    logger.info(f"[组合执行] 本轮执行 {len(execution_plan)} 笔, 淘汰 {pc.candidate_count - len(execution_plan)} 个候选")


# ============================================================
#  核心决策管线: _process_symbol_impl
# ============================================================

_LEGACY_PIPELINE_WARNED = False  # 进程内仅告警一次, 避免日志洪流


def _use_new_pipeline() -> bool:
    """信号管线路由决策

    2026-05-31 (Task #7): 默认使用 CRA 新管线 (modular stages),
    旧管线保留为 fallback (仅当配置显式关闭时启用)。

    配置优先级 (任一为 False 时禁用新管线):
      1. pipeline.use_modular_stages (官方配置, 默认 True)
      2. pipeline.use_new_stages      (旧字段, 向后兼容, 默认 True)

    使用旧管线时输出 DEPRECATED 警告 (进程内仅一次)。
    """
    global _LEGACY_PIPELINE_WARNED
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        # 新字段优先, 缺省回退到旧字段; 两者均默认 True (CRA 优先)
        modular = cfg.get_dotted("pipeline.use_modular_stages", None)
        if modular is None:
            modular = cfg.get_dotted("pipeline.use_new_stages", True)
        modular = bool(modular)
    except Exception:
        # 配置读取异常时也默认走 CRA, 防止退化为旧链路
        modular = True

    if not modular and not _LEGACY_PIPELINE_WARNED:
        logger.warning(
            "[DEPRECATED] 正在使用旧信号管线, 建议切换到 CRA 模式 "
            "(pipeline.use_modular_stages=true). 旧管线已进入退役窗口, 后续版本将移除."
        )
        _LEGACY_PIPELINE_WARNED = True
    return modular


def _process_symbol_new_pipeline(scheduler, symbol: str, vix: float,
                                  spy_context: Dict[str, Any], market_sentiment: float):
    """模块化 3-stage 管线 — 替代旧 _process_symbol_impl 的直接下单路径

    管线链: symbol → SymbolContext → SignalCandidate → OrderRequest
    最终委托给现有 _submit_order 执行, 保持 PDT/金字塔/执行逻辑不变。
    """
    from app.pipeline.stages.runner import build_default_pipeline
    from app.core.pipeline_contracts import SymbolContext, OrderRequest
    from app.core.models import SignalScore, Direction as SignalDirection

    # ── 构建 shared_state (从 scheduler 提取现有依赖) ──
    earnings_guard = getattr(scheduler, 'earnings_risk_guard', None)
    shared_kernel = None
    try:
        from app.core.shared_kernel import get_shared_kernel
        shared_kernel = get_shared_kernel()
    except Exception:
        pass

    battle_plan_manager = None
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        if mgr._plan:
            battle_plan_manager = mgr
    except Exception:
        pass

    shared_state = {
        "data_router": scheduler.market_data,
        "vix": vix,
        "spy_context": spy_context,
        "earnings_guard": earnings_guard,
        "shared_kernel": shared_kernel,
        "risk_engine": scheduler.risk_engine,
        "position_store": scheduler.position_store,
        "battle_plan_manager": battle_plan_manager,
        "voting_coordinator": getattr(scheduler, 'voting_coordinator', None),
        "execution_engine": scheduler.execution_engine,
        "polling_controller": getattr(scheduler, 'polling_controller', None),
    }

    # ── 执行管线 ──
    runner = build_default_pipeline(shared_state)
    result = runner.run(symbol)

    # ── 更新自适应轮询结果 ──
    if scheduler.polling_controller:
        md_stage = next((s for s in result.stages if s.stage_name == "market_data" and s.success), None)
        if md_stage and isinstance(md_stage.output, SymbolContext):
            ctx = md_stage.output
            scheduler.polling_controller.update_result(symbol, ctx.price, ctx.change_pct)
        else:
            scheduler.polling_controller.update_result(symbol, 0, 0)

    # ── 无最终输出 → 返回 (被拦截/无信号) ──
    if result.aborted or result.final_output is None:
        if result.aborted:
            logger.info(f"[新管线] {symbol} 中止: {result.abort_reason}")
        return

    order_req = result.final_output
    candidate = order_req.candidate

    # ── 构建 SignalScore (桥接新契约 → 现有 _submit_order) ──
    # P0 修复 (2026-05-07): 添加 trace_id 供 trade_attribution 使用
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import uuid as _uuid
    # R8 修复 (2026-05-17): 优先用 candidate 已有的 trace_id (来自 strategy/pure._enrich_candidate)
    # 防止双重生成 trace_id 导致 attribution 行不匹配
    _trace = getattr(candidate, "trace_id", "") or (
        f"US-{_dt.now(_tz(_td(hours=-4))).strftime('%Y%m%d')}-{_uuid.uuid4().hex[:6].upper()}"
    )
    direction = SignalDirection.LONG if candidate.is_long else SignalDirection.SHORT
    signal = SignalScore(
        symbol=symbol,
        direction=direction,
        strength=candidate.strength,
        confidence=candidate.confidence,
        strategy_id=candidate.strategy_id,
        trace_id=_trace,
        metadata={
            "source": candidate.source,
            "rationale": candidate.rationale,
            "entry_zone": candidate.entry_zone,
            "risk_checks": order_req.risk_checks_passed,
            "risk_notes": order_req.risk_notes,
            "stop_loss": order_req.stop_loss,
            "take_profit": [tp["price"] for tp in order_req.take_profit_levels],
            # R8 修复 (2026-05-17): 透传归因字段 → T2 _insert_trade_attribution 能写 factor_*
            "_factor_detail": getattr(candidate, "factor_detail", {}) or {},
            "signal_tags": "/".join(getattr(candidate, "signal_tags", []) or []) or candidate.strategy_id,
            "factors_used": getattr(candidate, "signal_tags", []) or [],
            # P1 VWAP Gate (2026-05-24): 从 full market context 注入 VWAP 偏离度
            "vwap_deviation": _get_vwap_deviation(scheduler, symbol),
        },
    )

    # ── 构建 context dict (桥接 SymbolContext → 旧格式) ──
    ctx = candidate.context
    context = {}
    if ctx:
        context = {
            "symbol": symbol,
            "price": ctx.price,
            "bid": ctx.bid,
            "ask": ctx.ask,
            "lv3_bid": ctx.bid,
            "lv3_ask": ctx.ask,
            "prev_close": ctx.prev_close,
            "change_percent": ctx.change_pct * 100,
            "volume": ctx.volume,
            "avg_volume": ctx.avg_volume,
            "rsi": ctx.rsi,
            "macd": ctx.macd,
            "macd_signal": ctx.macd_signal,
            "bollinger_upper": ctx.bollinger_upper,
            "bollinger_lower": ctx.bollinger_lower,
            "supertrend_direction": ctx.supertrend_direction,
            "atr": ctx.atr,
            "atr_pct": ctx.atr_pct,
            "ma5": ctx.ma5,
            "ma20": ctx.ma20,
            "ma50": ctx.ma50,
            "ma200": ctx.ma200,
            "spread_bps": ctx.spread_bps,
            "adv_shares": ctx.adv_shares,
            "vix": ctx.vix,
            "vix_regime": ctx.vix_regime,
            "spy_price": ctx.spy_price,
            "spy_ma200": ctx.spy_ma200,
            "market_sentiment": ctx.market_sentiment,
            "news_sentiment": ctx.news_sentiment,
            "is_earnings_period": ctx.earnings_blackout,
            "is_earnings_blackout": ctx.earnings_blackout,
            "data_source": ctx.source,
        }
    else:
        # fallback: context from raw data_router
        try:
            context = scheduler.market_data.get_full_market_context(symbol) or {}
            context["symbol"] = symbol
        except Exception:
            context = {"symbol": symbol, "price": 0}

    # ── 构建 voting_result (最小结构, 满足 _submit_order 兼容性) ──
    voting_result = {
        "symbol": symbol,
        "direction": candidate.direction,
        "final_strength": candidate.strength,
        "consensus_level": candidate.confidence,
        "final_confidence": candidate.confidence,
        "std_dev": 0,
        "strategy_id": candidate.strategy_id,
        "position_suggestion": {
            "stop_loss_pct": (order_req.stop_loss / ctx.price - 1) if ctx and ctx.price > 0 and order_req.stop_loss > 0 else 0.05,
            "take_profit_pct": (order_req.take_profit_levels[0]["price"] / ctx.price - 1) if ctx and ctx.price > 0 and order_req.take_profit_levels else 0.10,
            "quantity": order_req.quantity,
        },
    }

    # ── P0 修复 (2026-05-07): 新管线也需要记录决策到 decisions.db ──
    vc = shared_state.get("voting_coordinator")
    if vc and hasattr(vc, '_record_decision_to_db'):
        try:
            vc._record_decision_to_db(voting_result, context, external_votes=None)
        except Exception as _rec_err:
            logger.debug(f"[新管线] 决策记录失败 (不影响主流程): {_rec_err}")

    # ── 委托给现有执行路径 (PDT → Gates → Pyramid → Execute) ──
    from app.pipeline.order_executor import _submit_order
    lock = getattr(scheduler, '_open_position_lock', None)
    if lock:
        lock.acquire()
    try:
        _submit_order(scheduler, symbol, signal, context, voting_result)
    finally:
        if lock:
            lock.release()
def _process_symbol_impl(scheduler, symbol: str, vix: float, spy_context: Dict[str, Any], market_sentiment: float,
                         on_candidate: callable = None):
    """P3 压缩管线 (2026-04-29): 宏观闸门 → 信号生成 → 仓位映射

    三层决策:
    1. 宏观闸门: VIX>=25 → 直接拒绝; SPY<MA200 → context flag (LONG 由 pre_trade_check 拦截)
    2. 信号生成: 盘中纯技术面 / 盘前盘后 LLM 投票
    3. 仓位映射: ATR → 仓位大小，不做信号二次衰减

    P2 2026-05-01: on_candidate 回调 — 传入时收集候选而非立即下单,
    供 PortfolioConstructor 统一排序分配。

    P2 2026-05-05: use_new_stages — 启用后委托给模块化 3-stage 管线
    (market_data → signal → risk), 仅替换直接下单路径, on_candidate 路径保持不变。
    """
    # ── Feature Flag: 新管线 ──
    if on_candidate is None and _use_new_pipeline():
        _process_symbol_new_pipeline(scheduler, symbol, vix, spy_context, market_sentiment)
        return

    logger.info(f"[决策] 开始处理 {symbol}")

    # ── Layer 1: 宏观闸门 (P3 2026-04-29) ──
    # VIX>=25: 硬拦截 (与 pre_trade_check.py P1 gate 对齐)
    if vix >= 25:
        logger.warning(f"[宏观闸门] VIX={vix:.1f}>=25, 禁止开仓")
        return

    # SPY < MA200: 仅设置 context flag, 不做硬拦截。
    # 方向感知的 LONG-only 拦截由 pre_trade_check.py P1 gate 权威执行,
    # SHORT 候选不受此限制。
    spy_price = spy_context.get("spy_price", 0)
    spy_ma200 = spy_context.get("spy_ma200", 0)
    spy_below_ma200 = bool(spy_price > 0 and spy_ma200 > 0 and spy_price < spy_ma200)
    if spy_below_ma200:
        logger.info(f"[宏观闸门] SPY ${spy_price:.2f} < MA200 ${spy_ma200:.2f}, "
                    f"设置 spy_below_ma200 flag (LONG 由 pre_trade_check 拦截, SHORT 放行)")

    # Step1: 获取行情上下文
    context = scheduler.market_data.get_full_market_context(symbol)
    if not context or context.get("price", 0) <= 0:
        logger.warning(f"[决策] {symbol} 行情数据不可用，跳过")
        if scheduler.polling_controller:
            scheduler.polling_controller.update_result(symbol, 0, 0)
        return

    context["symbol"] = symbol
    context["spy_below_ma200"] = spy_below_ma200
    context["spy_price"] = spy_price
    context["spy_ma200"] = spy_ma200

    # 动态成本参数注入
    price = context.get("price", 0)
    bid = context.get("lv3_bid", context.get("bid", 0))
    ask = context.get("lv3_ask", context.get("ask", 0))
    if bid > 0 and ask > 0 and price > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_bps = (ask - bid) / mid * 1e4
            context["spread_bps"] = round(spread_bps, 2)
    volume = context.get("volume", 0)
    if volume and volume > 0:
        context["adv_shares"] = volume * 3

    if scheduler.polling_controller:
        price = context.get("price", 0)
        change_pct = context.get("change_percent", 0) / 100.0 if context.get("change_percent") else 0
        scheduler.polling_controller.update_result(symbol, price, change_pct, context)

    # 财报感知注入
    earnings_guard = getattr(scheduler, 'earnings_risk_guard', None)
    if earnings_guard:
        earnings_status = earnings_guard.get_earnings_status(symbol)
        context["earnings_status"] = earnings_status
        context["is_earnings_period"] = earnings_status.get("is_earnings_period", False)
        context["is_earnings_blackout"] = earnings_status.get("is_blackout", False)
        context["earnings_atr_multiplier"] = earnings_status.get("atr_multiplier", 2.5)
        if earnings_status.get("is_blackout") and not scheduler.position_store.has(symbol):
            logger.info(f"[财报拦截] {symbol} 处于禁建期: {earnings_status.get('blackout_reason', '')}")
            return

    # VIX Regime 注入 (通过 SharedKernel SSOT, 两个引擎共用)
    try:
        from app.core.shared_kernel import get_shared_kernel
        kernel = get_shared_kernel()
        multipliers = kernel.get_vix_multipliers()
        context["vix_regime"] = multipliers["regime"]
        context["vix_position_multiplier"] = multipliers["position_multiplier"]
        context["vix_stop_loss_adjustment"] = multipliers["stop_loss_multiplier"]
    except Exception as e:
        logger.debug("[VIX Regime] 注入失败 (使用默认值): %s", e)
        context["vix_regime"] = "normal"
        context["vix_position_multiplier"] = 1.0
        context["vix_stop_loss_adjustment"] = 1.0

    # 作战计划快车道
    battle_plan_match = None
    if not scheduler.position_store.has(symbol):
        try:
            from app.us_pipeline.battle_plan_manager import USBattlePlanManager
            mgr = USBattlePlanManager()
            target = mgr.get_target_info(symbol)
            if target:
                if target.get("status") == "CANCELLED":
                    logger.info(f"🚫 [作战计划] {symbol} 计划已取消，拒绝新仓")
                    return
                if target.get("status") == "ACTIVE":
                    current_price = context.get("price", 0)
                    entry_zone = target.get("entry_zone", [])
                    direction = target.get("direction", "").upper()
                    rsi = context.get("rsi", 0)
                    is_technical_match = False
                    match_reason = ""
                    if entry_zone and len(entry_zone) == 2:
                        low, high = entry_zone
                        if direction == "SHORT":
                            if low <= current_price <= high:
                                is_technical_match = True
                                match_reason = f"计划内做空 | 价格 {current_price:.2f} ∈ [{low}-{high}]"
                                if rsi < 40:
                                    match_reason += f" | ⚠️ RSI {rsi:.0f} 偏低(做空偏早)"
                        elif direction == "LONG":
                            if low <= current_price <= high:
                                is_technical_match = True
                                match_reason = f"计划内做多 | 价格 {current_price:.2f} ∈ [{low}-{high}]"
                                if rsi > 60:
                                    match_reason += f" | ⚠️ RSI {rsi:.0f} 偏高(做多偏晚)"
                    if is_technical_match:
                        logger.warning(
                            f"🚀 [快车道] {symbol} 匹配作战计划技术指标!\n"
                            f"   原因: {match_reason}\n   动作: 跳过投票/LLM，直接执行"
                        )
                        battle_plan_match = target
        except Exception as e:
            logger.debug(f"[作战计划] 检查异常 (放行): {e}")

    # P0: 检查 restricted 列表 (2026-05-02 修复: NVDA restricted 违规-$1302)
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        _bp_mgr = USBattlePlanManager()
        # P2 (2026-05-26): 不再每次 symbol 重新 load 文件, singleton 已缓存
        if not _bp_mgr._plan:
            _bp_mgr.load(_bp_mgr.plan_path)
        _plan = _bp_mgr.get_plan()
        _restricted = _plan.get("restricted", []) if _plan else []
        if symbol in _restricted:
            logger.warning(f"🚫 [Restricted拦截] {symbol} 在限制列表中, 拒绝交易")
            return
    except Exception as e:
        logger.debug(f"[Restricted检查] 异常 (放行): {e}")

    # 执行快车道逻辑
    if battle_plan_match:
        try:
            from app.core.models import SignalScore, Direction as SignalDirection
            from app.risk.capital_safety import get_capital_safety_system

            plan_dir = battle_plan_match.get("direction", "LONG").upper()
            signal_direction = SignalDirection.LONG if plan_dir == "LONG" else SignalDirection.SHORT
            strength = int(float(battle_plan_match.get("confidence", 0.7)) * 100)

            # T9b (2026-05-16): 把 LLM Plan 的 factors_used 透传到 signal.metadata
            # 后续 _insert_trade_attribution 写入 trade_attribution.signal_tags
            _factors_used = battle_plan_match.get("factors_used", []) or []
            _signal_tags = "/".join(_factors_used) if _factors_used else "llm_plan"

            signal = SignalScore(
                symbol=symbol, direction=signal_direction, strength=strength,
                confidence=0.85,
                strategy_id="BATTLE_PLAN_FAST_PATH",
            )
            # 注入 metadata 供下游 trade_attribution 落库
            if hasattr(signal, "metadata") and signal.metadata is not None:
                signal.metadata["factors_used"] = _factors_used
                signal.metadata["signal_tags"] = _signal_tags
                signal.metadata["catalyst"] = battle_plan_match.get("catalyst", "")
            else:
                signal.metadata = {
                    "factors_used": _factors_used,
                    "signal_tags": _signal_tags,
                    "catalyst": battle_plan_match.get("catalyst", ""),
                }

            try:
                from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskAction
                sm = RiskStateMachine()
                if not sm.is_trade_allowed(RiskAction.OPEN):
                    logger.warning(f"🚫 [快车道·风控拦截] {symbol} 系统处于 {sm.get_state().value}")
                    return
            except Exception as e:
                logger.error(f"⚠️ [快车道] 风险状态机检查失败 (放行): {e}")

            try:
                capital = get_capital_safety_system()
                allowed, reason = capital.can_open_position()
                if not allowed:
                    logger.warning(f"[资本安全] 拦截 {symbol} (计划内): {reason}")
                    return
            except Exception as e:
                logger.error(f"[资本安全] 检查异常，安全起见拦截交易: {e}")
                return

            if scheduler.risk_engine:
                if not scheduler.risk_engine.pre_trade_check(signal):
                    logger.info(f"[风控] 拦截 {symbol} (计划内)")
                    return

            # 委托下单 (延迟导入避免循环依赖)
            # P0: 跨引擎互斥锁保护开仓临界区
            # P0-fix: 补传 vote_result 参数; fast-path 无投票数据故传 None
            from app.pipeline.order_executor import _submit_order
            lock = getattr(scheduler, '_open_position_lock', None)
            if lock:
                lock.acquire()
            try:
                _submit_order(scheduler, symbol, signal, context, None)
            finally:
                if lock:
                    lock.release()
            return
        except Exception as e:
            logger.error(f"[快车道] {symbol} 执行异常: {e}", exc_info=True)
            return

    # 非快车道：门禁检查
    if not battle_plan_match:
        if not scheduler.position_store.has(symbol):
            try:
                from app.us_pipeline.battle_plan_manager import USBattlePlanManager
                mgr = USBattlePlanManager()
                target = mgr.get_target_info(symbol)
                if target and target.get("status") == "CANCELLED":
                    logger.info(f"🚫 [作战计划] {symbol} 计划已取消，拒绝新仓")
                    return
            except Exception as e:
                logger.debug(f"[作战计划] 检查异常 (放行): {e}")

    # Step2: 注入新闻情绪
    try:
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        current_phase = get_market_phase()
    except Exception:
        current_phase = None

    if current_phase == MarketPhase.TRADING:
        news_sentiment = 0.0
        logger.debug(f"[情绪·盘中] {symbol} 跳过新闻获取 (TECHNICAL_ONLY)")
    else:
        rss_sentiment = scheduler.news_provider.get_sentiment_score(symbol)
        fh_sentiment = scheduler.data_router.get_sentiment_score(symbol)
        if rss_sentiment != 0.0 and fh_sentiment != 0.0:
            news_sentiment = round((rss_sentiment * 0.6 + fh_sentiment * 0.4), 3)
        else:
            news_sentiment = rss_sentiment if rss_sentiment != 0.0 else fh_sentiment
        logger.debug(f"[情绪] {symbol} rss={rss_sentiment:.2f} finnhub={fh_sentiment:.2f} merged={news_sentiment:.2f}")
    context["news_sentiment"] = news_sentiment
    context["vix"] = vix
    context.update({
        "spy_price": spy_context.get("spy_price", context.get("spy_price", 0)),
        "spy_ma20": spy_context.get("spy_ma20", 0),
        "spy_ma50": spy_context.get("spy_ma50", 0),
        "spx_change_percent": spy_context.get("spx_change_percent", 0),
        "qqq_change_percent": spy_context.get("qqq_change_percent", 0),
        "price_change_5d": spy_context.get("price_change_5d", 0),
    })

    # 做空信号触发检测
    rsi = context.get("rsi", 0)
    macd = context.get("macd", 0)
    price = context.get("price", 0)
    ma20 = context.get("ma20", 0)
    change_pct = context.get("change_percent", 0)
    short_trigger = False
    short_reasons = []
    if rsi > 85:
        short_trigger = True
        short_reasons.append(f"RSI 极端超买 {rsi:.1f}")
    if ma20 > 0 and price > ma20 * 1.15 and rsi > 75:
        short_trigger = True
        short_reasons.append(f"价格偏离 MA20 +{(price/ma20-1)*100:.1f}% + RSI {rsi:.1f}")
    if macd < 0 and rsi > 70:
        short_trigger = True
        short_reasons.append(f"MACD 死叉 {macd:.2f} + RSI {rsi:.1f}")
    if change_pct > 5.0 and rsi > 70:
        short_trigger = True
        short_reasons.append(f"单日暴涨 +{change_pct:.1f}% + RSI {rsi:.1f}")
    if short_trigger:
        logger.warning(f"[做空触发] {symbol}: {'; '.join(short_reasons)} → 强制注入 SHORT 信号")
        context["short_signal_triggered"] = True
        context["short_reasons"] = short_reasons
        if news_sentiment > 0:
            context["news_sentiment"] = -news_sentiment * 0.5
            logger.info(f"[做空触发] {symbol} 新闻情绪反转为 {context['news_sentiment']:.2f}")

    # Step3: 信号生成 (P3: market_regime_filter 已由宏观闸门替代，不再做二次衰减)
    if current_phase == MarketPhase.TRADING:
        vote_result = _generate_technical_signal(scheduler, context)
    else:
        vote_result = scheduler.voting_coordinator.run_voting(context, external_votes=[])
        logger.info(
            f"[投票] {symbol} strength={vote_result['final_strength']} "
            f"consensus={vote_result['consensus_level']:.2f} direction={vote_result.get('direction', 'LONG')}"
        )

    # 金字塔加仓检测
    direction = vote_result.get("direction", "LONG")
    if scheduler.position_store.has(symbol) and direction == "LONG":
        try:
            from app.risk.pyramid_engine import get_pyramid_engine
            pyramid = get_pyramid_engine()
            position = scheduler.position_store.get(symbol)
            price = context.get("price", 0)
            max_total_size = float(context.get("max_position_size", 50.0))
            can_add, add_reason, add_params = pyramid.can_add_position(
                position=position, current_price=price, max_total_size=max_total_size,
            )
            if can_add:
                logger.info(f"[金字塔加仓] {symbol} 满足加仓条件: {add_reason} | 加仓 {add_params['add_size']:.0f} 股 @ ${price:.2f}")
                if not hasattr(vote_result, 'get') or not vote_result.get('is_add_on'):
                    vote_result['is_add_on'] = True
                    vote_result['add_on_params'] = add_params
            else:
                logger.debug(f"[金字塔加仓] {symbol} 不满足加仓条件: {add_reason}")
        except Exception as e:
            logger.warning(f"[金字塔加仓] {symbol} 检测异常: {e}")

    # 一致性检查
    if current_phase != MarketPhase.TRADING:
        logger.info(
            f"[投票] {symbol} strength={vote_result['final_strength']} "
            f"consensus={vote_result['consensus_level']:.2f} "
            f"direction={vote_result.get('direction', 'LONG')} "
            f"(threshold={scheduler.MIN_CONSENSUS_LEVEL})"
        )
        # ── P0 修复 (2026-05-02): 盘前/盘后禁止新开仓 ──
        # 只有 TRADING 时段允许新建持仓。盘前/盘后仅允许已有持仓的退出监控。
        if not scheduler.position_store.has(symbol):
            logger.info(
                f"[时段门禁] {symbol} 当前阶段={current_phase.value} 且无现有持仓，"
                f"禁止新建仓位 (等待 TRADING 时段)"
            )
            get_rejection_logger().record(symbol, f"phase={current_phase.value}",
                "PHASE_BLOCKED", strength=vote_result['final_strength'],
                confidence=None, direction=vote_result.get('direction'))
            return
    if vote_result["consensus_level"] < scheduler.MIN_CONSENSUS_LEVEL:
        if current_phase != MarketPhase.TRADING:
            logger.info(f"[投票] {symbol} 模型意见分歧 (consensus={vote_result['consensus_level']:.2f}<{scheduler.MIN_CONSENSUS_LEVEL})，放弃")
        get_rejection_logger().record(symbol, f"consensus={vote_result['consensus_level']:.2f}",
            "CONSENSUS_LOW", strength=vote_result['final_strength'],
            confidence=None, direction=vote_result.get('direction'))
        return

    if vote_result.get("is_neutral", False):
        if current_phase != MarketPhase.TRADING:
            logger.info(f"[投票] {symbol} 中性信号 (strength={vote_result['final_strength']} std={vote_result['std_dev']:.1f})，无明确方向，跳过")
        get_rejection_logger().record(symbol, f"neutral std={vote_result.get('std_dev', 0):.1f}",
            "NEUTRAL", strength=vote_result['final_strength'],
            confidence=None, direction=vote_result.get('direction'))
        return

    # Step4: 构建 SignalScore
    signal = _build_signal_from_vote(scheduler, symbol, vote_result, context)

    # 信号强度过滤
    # P0 修复 (2026-04-30): 做空 strength 本身就是做空强度，不应反转。
    # 旧逻辑 effective = 100 - strength 导致 strength=80 → effective=20 被错误拦截。
    # 做空专用门禁已在 _check_gates_and_conflicts 中实现 (strength<30 绝对底限 + effective>=65)。
    effective_strength = signal.strength
    if effective_strength < scheduler.MIN_SIGNAL_STRENGTH:
        logger.info(f"[决策] {symbol} 信号强度不足 (direction={direction} strength={signal.strength} effective={effective_strength}<{scheduler.MIN_SIGNAL_STRENGTH})，放弃")
        get_rejection_logger().record(symbol,
            f"effective={effective_strength}<{scheduler.MIN_SIGNAL_STRENGTH}",
            "STRENGTH_WEAK", strength=signal.strength,
            confidence=signal.confidence, direction=direction)
        return

    # ── Degradation-aware + direction-aware confidence gate ──
    # Base threshold: scheduler.MIN_CONFIDENCE (已含 DEGRADED 模式上调)
    # degraded_confidence_offset: 降级信号额外降低阈值 (config 默认 -0.1)
    # direction_bias: LONG 需 0.75, SHORT 需 0.60 (从 yaml 读取)
    effective_min_conf = scheduler.MIN_CONFIDENCE
    try:
        from app.core.config_container import get_config as _get_cfg
        _cfg = _get_cfg()
    except Exception:
        _cfg = None

    # Degraded offset: 降级信号允许更低的置信度
    _sig_meta = signal.metadata if isinstance(signal.metadata, dict) else {}
    is_degraded = bool(_sig_meta.get("degraded", False))
    if is_degraded and _cfg:
        degraded_offset = float(_cfg.get_dotted('strategies.defaults.degraded_confidence_offset', -0.1) or -0.1)
        effective_min_conf += degraded_offset

    # Direction-aware threshold: 从 yaml 的 direction_bias 读取方向专用阈值
    if _cfg:
        direction_str = signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction)
        dir_bias_key = f'strategies.defaults.direction_bias.{direction_str}.min_confidence'
        dir_threshold = _cfg.get_dotted(dir_bias_key, None)
        if dir_threshold is not None:
            effective_min_conf = max(effective_min_conf, float(dir_threshold))

    if signal.confidence < effective_min_conf:
        logger.info(f"[决策] {symbol} 置信度不足 ({signal.confidence:.2f}<{effective_min_conf:.2f}, "
                    f"base={scheduler.MIN_CONFIDENCE:.2f}, degraded={is_degraded})，放弃")
        get_rejection_logger().record(symbol,
            f"confidence={signal.confidence:.2f}<{effective_min_conf:.2f}",
            "CONFIDENCE_LOW", strength=signal.strength,
            confidence=signal.confidence, direction=direction)
        return

    # ====== P0 修复 (2026-05-04): 信号持久化到 signals 表 ======
    # 修复前: signals 表永远为空 (0 rows)，信号→成交归因链路断链
    # 修复后: 所有通过强度/置信度过滤的信号都写入 signals 表
    try:
        import sqlite3
        from datetime import datetime as _dt
        from app.core.paths import get_trade_records_db
        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO signals "
            "(signal_date, symbol, direction, signal_strength, confidence, "
            " entry_price, stop_loss, take_profit, strategy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _dt.now().strftime("%Y-%m-%d"),
                symbol,
                direction,
                signal.strength,
                signal.confidence,
                context.get("price", 0.0),
                vote_result.get("position_suggestion", {}).get("stop_loss_pct", 0),
                vote_result.get("position_suggestion", {}).get("take_profit_pct", 0),
                vote_result.get("strategy_id", "voting_llm"),
                "PENDING",
            ),
        )
        conn.commit()
        conn.close()
        logger.info(f"[P0·信号持久化] {symbol} 已写入 signals 表: strength={signal.strength} confidence={signal.confidence:.2f}")
    except Exception as e:
        logger.error(f"[P0·信号持久化] {symbol} 写入 signals 表失败: {e}")
    # ============================================================

    # Step5: 风控检查 (P2 2026-05-01: 收集模式下跳过, 由 _check_gates_and_conflicts 统一执行)
    if on_candidate is not None:
        # 收集模式: 仅做轻量资本安全预检，完整门禁在 _submit_order 时执行
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            allowed, reason = capital.can_open_position()
            if not allowed:
                logger.warning(f"[资本安全] {symbol} 禁止开仓: {reason}")
                from app.pipeline.position_monitor import _notify_risk_blocked
                _notify_risk_blocked(scheduler, symbol, f"capital_safety: {reason}")
                return
        except Exception as e:
            logger.warning(f"[资本安全] 检查异常 (继续执行): {e}")
        on_candidate(symbol, signal, context, vote_result)
        return

    # 直接下单模式 (向后兼容: 作战计划快车道等)
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()
        allowed, reason = capital.can_open_position()
        if not allowed:
            logger.warning(f"[资本安全] {symbol} 禁止开仓: {reason}")
            from app.pipeline.position_monitor import _notify_risk_blocked
            _notify_risk_blocked(scheduler, symbol, f"capital_safety: {reason}")
            return
    except Exception as e:
        logger.warning(f"[资本安全] 检查异常 (继续执行): {e}")

    if scheduler.us_risk_pipeline:
        try:
            pipeline_ctx = {
                "risk_engine": scheduler.risk_engine,
                "position_store": scheduler.position_store,
                "vix": context.get("vix", 0),
                "spy_change_pct": spy_context.get("spx_change_percent", 0),
                "symbol": symbol,
            }
            pipeline_result = scheduler.us_risk_pipeline.run(signal, pipeline_ctx)
            if not pipeline_result.passed:
                logger.warning("[USRiskPipeline] ❌ %s 被 %s 拦截: %s",
                    symbol, pipeline_result.rejected_by, pipeline_result.reason)
                from app.pipeline.position_monitor import _notify_risk_blocked
                _notify_risk_blocked(scheduler, symbol, f"us_risk_pipeline: {pipeline_result.rejected_by}")
                return
            logger.debug("[USRiskPipeline] ✅ %s 全部通过 (%d/%d)",
                symbol, pipeline_result.checks_passed, pipeline_result.checks_run)
        except Exception as e:
            logger.warning("[USRiskPipeline] 执行异常 (降级到旧 RiskEngine): %s", e)

    if scheduler.risk_engine:
        try:
            if not scheduler.risk_engine.pre_trade_check(signal):
                logger.info(f"[风控] {symbol} 预交易检查未通过")
                from app.pipeline.position_monitor import _notify_risk_blocked
                _notify_risk_blocked(scheduler, symbol, "pre_trade_check failed")
                return
        except Exception as e:
            logger.warning(f"[风控] {symbol} 检查异常：{e}")

    # Step6: 下单 (P0: 跨引擎互斥锁保护开仓临界区)
    from app.pipeline.order_executor import _submit_order
    lock = getattr(scheduler, '_open_position_lock', None)
    if lock:
        lock.acquire()
    try:
        _submit_order(scheduler, symbol, signal, context, vote_result)
    finally:
        if lock:
            lock.release()


# ============================================================
#  投票结果 → SignalScore
# ============================================================
def _build_signal_from_vote(scheduler, symbol: str, vote_result: Dict[str, Any], context: Dict[str, Any]) -> SignalScore:
    """将投票结果转换为 SignalScore"""
    direction_str = vote_result.get("direction", "LONG")
    direction = Direction.LONG if direction_str == "LONG" else Direction.SHORT

    # Task #9 (2026-05-31): 降级信号置信度封顶 0.60
    is_degraded = bool(vote_result.get("degraded"))
    degradation_level = vote_result.get("degradation_level", 0)
    degradation_reason = vote_result.get("degradation_reason", "")
    raw_confidence = float(vote_result.get("final_confidence", 0.5) or 0.5)
    if is_degraded and raw_confidence > 0.60:
        logger.warning(
            f"[降级信号] {symbol} confidence {raw_confidence:.2f} → 0.60 "
            f"(level={degradation_level}, reason={degradation_reason})"
        )
        raw_confidence = 0.60

    # ── Signal Quality Scorer integration (2026-05-31) ──
    # Build a ctx-like object from the context dict so SignalQualityScorer can
    # read attributes (it uses getattr extensively). Take max of vote-based and
    # quality-based confidence to avoid being overly aggressive in either direction.
    quality_factors = None
    try:
        from app.strategy.signal_quality_scorer import SignalQualityScorer

        class _ScorerCtx:
            """Minimal attribute wrapper so SignalQualityScorer.score() works on a dict."""
            def __init__(self, d: dict):
                for k, v in d.items():
                    setattr(self, k, v)

        scorer_ctx = _ScorerCtx(context)
        # Ensure required attributes have safe defaults
        for _attr, _default in (
            ('symbol', ''), ('price', 0), ('ma5', 0), ('ma20', 0), ('ma50', 0),
            ('rsi', 50), ('macd', 0), ('macd_signal', 0), ('volume', 0),
            ('avg_volume', 0), ('vix', 20), ('vix_regime', 'normal'),
            ('spy_price', 0), ('spy_ma200', 0), ('market_regime', ''),
            ('trend', ''), ('volume_ratio', 1.0), ('slope_5m', 0.0),
            ('order_flow_imbalance', 0.0), ('ticker_buy_sell_ratio', 1.0),
            ('metadata', {}),
        ):
            if not hasattr(scorer_ctx, _attr):
                setattr(scorer_ctx, _attr, _default)

        quality = SignalQualityScorer.score(scorer_ctx, direction_str)
        raw_confidence = max(raw_confidence, quality.confidence)
        quality_factors = quality.to_dict()
    except Exception as e:
        logger.debug(f"[SignalPipeline] quality scoring failed: {e}")

    regime_raw = vote_result.get("market_regime", context.get("market_regime", "RANGE"))
    # market_regime 可能来自多个路径:
    # 1. vote_result["market_regime"] 可能是字符串 (如 "TREND_UP") 或字典 (如 {"regime": "NORMAL", ...})
    # 2. context["market_regime"] 可能是 IntradayScorer 输出的字符串
    # 3. 兜底值为 "RANGE"
    if isinstance(regime_raw, dict):
        regime_str = regime_raw.get("regime", "RANGE")
    else:
        regime_str = str(regime_raw) if regime_raw else "RANGE"
    # 规范化: 将可能的中文/非标准值映射到 MarketRegime 枚举
    _REGIME_MAP = {
        "正常": "RANGE", "neutral": "RANGE", "NORMAL": "RANGE",
        "上涨": "TREND_UP", "bullish": "TREND_UP",
        "下跌": "TREND_DOWN", "bearish": "TREND_DOWN",
        "高波动": "HIGH_VOL", "high_volatility": "HIGH_VOL",
        "低波动": "LOW_VOL", "low_volatility": "LOW_VOL",
    }
    regime_str = _REGIME_MAP.get(regime_str, regime_str)
    try:
        regime = MarketRegime(regime_str)
    except ValueError:
        logger.debug(f"[regime 规范化] 无法解析 '{regime_str}'，降级为 RANGE")
        regime = MarketRegime.RANGE

    # P1-1 (2026-05-14): phase-aware 过期 (opening 5min / midday 15min / closing 3min)
    from app.utils.datetime_utils import signal_expires_at
    expires_at = signal_expires_at()

    account_equity = 0.0
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cs = get_capital_safety_system()
        if cs._state and cs._state.current_equity > 0:
            account_equity = cs._state.current_equity
    except Exception:
        pass

    # P1-2 (2026-05-14): 注入当前持仓 — sector check 才能工作
    _current_positions = []
    try:
        ps = getattr(scheduler, "position_store", None)
        if ps is not None:
            for p in ps.all():
                _entry = float(getattr(p, "avg_entry_price", 0) or 0)
                _size = float(getattr(p, "size", 0) or 0)
                _current_positions.append({
                    "symbol": p.symbol,
                    "market_value": _entry * _size,
                    "owner_strategy": getattr(p, "owner_strategy", "main"),
                })
    except Exception:
        pass

    signal = SignalScore(
        signal_id=str(uuid.uuid4())[:8],
        trace_id=f"US-{now_et().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
        strategy_id="voting_llm",
        symbol=symbol,
        direction=direction,
        strength=int(vote_result.get("final_strength", 50)),
        confidence=float(raw_confidence),
        expected_horizon=60 * 24,
        volatility_fit=True,
        market_regime=regime,
        risk_reward_estimate=2.5,
        expires_at=expires_at,
        metadata={
            "consensus_level": vote_result.get("consensus_level", 0),
            "std_dev": vote_result.get("std_dev", 0),
            "news_sentiment": context.get("news_sentiment", 0),
            "vix": context.get("vix", 20),
            "model_scores": vote_result.get("model_scores", []),
            "decision_id": vote_result.get("decision_id"),
            "account_equity": account_equity,
            "_current_positions": _current_positions,  # P1-2: sector_check 用
            # T5 (2026-05-16): 把 IntradayScorer 的 _factor_detail 透传到 SignalScorer
            # SignalScorer 检测到 trend/volume 已上游计算 → 二层权重置 0, 避免双计
            "_factor_detail": vote_result.get("_factor_detail", {}) or {},
            # P1 VWAP Gate (2026-05-24): 透传 VWAP 偏离度供 pre_trade_check 过滤低强度多头
            "vwap_deviation": context.get("ticker_vwap_deviation"),
            # Task #9 (2026-05-31): 降级标记透传供下游使用
            "degraded": is_degraded,
            "degradation_level": degradation_level,
            "degradation_reason": degradation_reason,
            # Signal Quality Scorer (2026-05-31): 多维质量评分明细
            "quality_factors": quality_factors,
        },
    )
    return signal


# ============================================================
#  P4 2026-05-02: 动态选股池 — 风控驱动的选股过滤
# ============================================================

def _get_dynamic_pool_filter(scheduler) -> Optional[Dict[str, Any]]:
    """
    根据当前 tier + 风控等级生成动态选股池配置。

    Returns:
        None (不启用) 或 dict (包含 tier, risk_level, profile)
    """
    try:
        from app.risk.position_sizer import load_config_from_yaml
        from app.utils.dynamic_pool import generate_dynamic_pool_profile, load_pool_config
        from app.risk.futu_risk import calc_futu_risk
        from app.risk.risk_merger import merge_risk_levels, get_risk_action
        from app.risk.capital_safety import get_capital_safety_system

        pool_cfg = load_pool_config()
        if not pool_cfg.get("enable_dynamic_pool", True):
            return None

        # 获取当前水位状态
        capital = get_capital_safety_system()
        water_level = capital._state.water_level.value
        equity = capital._state.current_equity

        # 获取 tier
        tiers, _, _ = load_config_from_yaml()
        tier_name, _ = _select_tier_from_config(equity, tiers)

        # 获取富途实时保证金状态
        futu_level = "safe"  # 默认
        try:
            if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
                adapter = scheduler.execution_engine.broker_adapter
                if adapter and adapter.is_connected():
                    account_info = adapter.get_account_info()
                    if account_info:
                        from app.risk.futu_risk import calc_futu_risk
                        futu_result = calc_futu_risk(account_info)
                        futu_level = futu_result.level.value
        except Exception as e:
            import logging
            logging.getLogger("ftseek.dynamic_pool").debug(f"富途保证金查询失败: {e}")

        # 融合风控等级
        merged_level, reason, detail = merge_risk_levels(futu_level, water_level)
        risk_level = merged_level.value

        # 生成动态池配置
        profile = generate_dynamic_pool_profile(tier_name, risk_level)
        profile_dict = profile.to_dict()

        import logging
        logger = logging.getLogger("ftseek.dynamic_pool")
        logger.info(
            f"[动态池] tier={tier_name} | equity=${equity:,.0f} | "
            f"futu={futu_level} | water={water_level} → risk={risk_level} | "
            f"max_symbols={profile.max_symbols} | min_confidence={profile.min_confidence} | "
            f"max_price=${profile.max_price} | catalyst={profile.require_catalyst}"
        )

        return {
            "tier": tier_name,
            "risk_level": risk_level,
            "profile": profile_dict,
            "action": get_risk_action(merged_level),
        }
    except Exception as e:
        import logging
        logging.getLogger("ftseek.dynamic_pool").debug(f"动态池生成失败 (使用默认): {e}")
        return None


def _select_tier_from_config(equity: float, tiers: dict) -> tuple:
    """S5: 委托给 position_sizer._select_tier，避免代码重复"""
    from app.risk.position_sizer import _select_tier
    return _select_tier(equity, tiers)


def _apply_pool_filter(scheduler, symbols: list, pool_filter: dict) -> list:
    """过滤 batch 中的股票"""
    filtered = []
    for symbol in symbols:
        if _passes_pool_filter(scheduler, symbol, pool_filter):
            filtered.append(symbol)
    return filtered


def _passes_pool_filter(scheduler, symbol: str, pool_filter: dict) -> bool:
    """检查单只股票是否满足当前动态池条件"""
    try:
        profile = pool_filter.get("profile", {})
        max_symbols = profile.get("max_symbols", 0)

        # 危险/熔断 → 停止开新仓
        if max_symbols == 0:
            return False

        # 已有持仓的股票不受过滤限制
        if scheduler.position_store.has(symbol):
            return True

        # 股价过滤 (防止高价股对小资金造成过大滑点)
        max_price = profile.get("max_price", 10000)
        min_price = profile.get("min_price", 0)
        try:
            if scheduler.market_data:
                quote = scheduler.market_data.get_quote(symbol)
                if quote:
                    price = quote.get("price", 0) or quote.get("last_price", 0)
                    if price > 0:
                        if price > max_price:
                            return False
                        if price < min_price:
                            return False
        except Exception:
            pass  # 无法获取价格时放行

        return True
    except Exception:
        return True  # 异常时默认放行


