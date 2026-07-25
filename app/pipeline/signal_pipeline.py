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


def _get_vwap_deviation(scheduler, symbol: str, cached_context: dict = None) -> Optional[float]:
    """M9 (2026-06-03): 从已获取的 context 读取 VWAP 偏离度, 避免重复 get_full_market_context。
    返回 None 表示数据不可用 (pre_trade_check 将软放行)。
    """
    try:
        # 优先使用已缓存的 context (H1 去重)
        if cached_context and "ticker_vwap_deviation" in cached_context:
            return float(cached_context["ticker_vwap_deviation"])
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
from app.coordinator.manual_hold_registry import manual_hold_registry

logger = logging.getLogger(__name__)

# ── Rapid Regime Change 历史缓冲区 (P0 2026-06-17) ──
# 保留最近 5 分钟的 VIX/SPY 采样, 供 check_rapid_regime_change 检测急变
_VIX_HISTORY: list = []   # [(timestamp, vix_value), ...]
_SPY_HISTORY: list = []   # [(timestamp, spy_value), ...]
_HISTORY_MAX_SECONDS: float = 360.0  # 保留最近 6 分钟 (略大于 5 分钟窗口)
_HISTORY_MAX_ENTRIES: int = 30       # 最多 30 个采样点


# ============================================================
# 决策引擎辅助 (Task #19)
# ============================================================
def _check_execution_rules(rules: dict, symbol: str, context: dict) -> bool:
    """检查 execution_rules 是否允许执行。

    返回 True=允许, False=阻止。规则缺失或无法判定时一律放行。
    """
    if not rules or not isinstance(rules, dict):
        return True

    try:
        prev_close = float(context.get("prev_close", 0) or 0)
        open_price = float(context.get("open_price", 0) or 0)
        current_price = float(context.get("current_price", context.get("price", 0)) or 0)
    except (TypeError, ValueError):
        return True

    if prev_close > 0 and open_price > 0:
        gap_pct = (open_price - prev_close) / prev_close

        # 高开超 2%
        if gap_pct > 0.02:
            action = str(rules.get("gap_up_over_2pct", "") or "").upper()
            if action == "SKIP":
                logger.info(
                    f"[ExecRules] {symbol} 高开{gap_pct*100:.1f}%, 规则SKIP"
                )
                return False

        # 低开超 2%
        if gap_pct < -0.02:
            action = str(rules.get("gap_down_over_2pct", "") or "").upper()
            if action == "WAIT_RECOVERY":
                # 检查现价是否已恢复至开盘价以上
                if current_price > 0 and current_price <= open_price:
                    logger.info(
                        f"[ExecRules] {symbol} 低开{gap_pct*100:.1f}%, "
                        f"等待恢复(现价{current_price:.2f}≤开盘{open_price:.2f})"
                    )
                    return False
                # 已恢复 → 允许

    # hit_zone_then_drop: 已在 entry_zone 内才会进入 Fast Lane, 这里默认通过
    return True


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

            # ── 双链路并行 (Task #3 2026-06-15): Dashboard 自动交易总开关 ──
            # 关闭时: 跳过本轮信号扫描 (信号生成/数据采集/退出监控不受影响)
            try:
                from app.web_dashboard.mode_switch_service import get_mode_switch_service
                if not get_mode_switch_service().is_auto_enabled():
                    logger.debug("[AUTO] Auto trading disabled via Dashboard, skip scan")
                    for _ in range(int(scheduler.SCAN_INTERVAL_MINUTES * 60)):
                        if not scheduler.running:
                            return
                        time.sleep(1)
                    continue
            except Exception as _ae_err:
                logger.debug(f"[AUTO] auto_enabled 检查异常 (放行): {_ae_err}")
            from app.core.scheduler_triggers import get_market_phase, MarketPhase
            phase = get_market_phase()
            if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                logger.info(f"[扫描] {phase.value} → 完全静默，跳过扫描")
                for _ in range(int(scheduler.SCAN_INTERVAL_MINUTES * 60)):
                    if not scheduler.running:
                        return
                    time.sleep(1)
                continue

            # 2026-06-28: TRADING 阶段新单执行由 playbook futu_simulate 独占.
            # scan_loop 仅做信号研究与 IC 数据采集, 不再下单, 避免与 Playbook 路径冲突.
            # 架构 (见 AGENT.md): playbook 为唯一下单/管理管线; 主管线新仓双重冻结
            #   (此处 _skip_execution_in_trading + order_executor._submit_order 白名单).
            # ⚠️ 勿 gate 掉整个 scan_loop 线程: 它还独家驱动日/周/月复盘、持仓快照、
            #   看门狗心跳、财报刷新 (见本函数下方 _check_*_review / _check_position_snapshot).
            _skip_execution_in_trading = True
            scheduler._skip_execution_in_trading = True

            if is_circuit_breaker_open():
                logger.warning("[熔断] API 熔断器已开启，暂停扫描...")
                for _ in range(int(scheduler.SCAN_INTERVAL_MINUTES * 60)):
                    if not scheduler.running:
                        return
                    time.sleep(1)
                continue

            # ── 数据新鲜度门禁 (2026-06-10): 主数据源不健康时不开新仓 ──
            # 根因: Futu Push 断连 → 降级到 TwelveData 5分钟缓存 → 在旧价格上生成信号 → 滑点亏损
            # 修复: 在决策入口检查主数据源健康状态，不健康则跳过入场（退出由 position_monitor 独立处理）
            if phase == MarketPhase.TRADING:
                _futu = getattr(scheduler.market_data, 'futu', None)
                if _futu and hasattr(_futu, 'is_healthy') and not _futu.is_healthy(log_change=False):
                    logger.warning("[扫描] ⚠️ Futu LV3 数据源不健康，跳过入场扫描 (退出监控独立运行)")
                    for _ in range(30):  # 30s 后重试
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
                # P-2 衍生项 (2026-07-24, DEB-20260724-da-chain-audit §7.5, 用户裁决):
                #   主线扫描本体静默化 — 近4日成交 100% PLAYBOOK、主线 0 笔, 扫描仅空转烧配额/CPU。
                #   只停 _run_signal_scan_impl 本体; 寄宿任务 (盘前/盘后巡检、日周月度复盘、
                #   持仓快照、watchdog) 全部保留。回滚: strategy.main_scan_enabled 改 true。
                _main_scan_on = True
                try:
                    from app.core.config_container import get_config as _get_cfg_ms
                    _main_scan_on = bool(_get_cfg_ms().get_dotted("strategy.main_scan_enabled", True))
                except Exception:
                    pass
                if not _main_scan_on:
                    logger.debug("[扫描] 主线扫描已静默 (strategy.main_scan_enabled=false), 寄宿任务照常")
                else:
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

    # 1. 开盘爆发期 (09:30-10:00): 主通道减少扫描 (防噪音)
    if 570 <= t < 600:
        return 10 * 60    # 10min (Reduced)

    # 2. 上午趋势期 (10:00-11:30): 主通道 Standard
    if 600 <= t < 690:
        return 5 * 60     # 5min (Standard)

    # 3. 午盘量谷期 (11:30-14:00): 主通道 Reduced
    if 690 <= t < 840:
        return 12 * 60    # 12min (Reduced)

    # 4. 下午趋势期 (14:00-15:30): 主通道 Standard
    if 840 <= t < 930:
        return 5 * 60     # 5min (Standard)

    # 5. 尾盘 MOC 窗口 (15:30-16:00): 主通道禁止开新仓
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

    # ── P0 (2026-06-17): Rapid Regime Change 检测 ──
    # 记录 VIX/SPY 历史采样, 检测 5 分钟内的急变 (VIX 涨>15% 或 SPY 跌>1%)
    _now_ts = time.time()
    _spy_price = spy_context.get("price", 0) or spy_context.get("spx_price", 0) or 0
    _VIX_HISTORY.append((_now_ts, float(vix)))
    if _spy_price > 0:
        _SPY_HISTORY.append((_now_ts, float(_spy_price)))
    # 清理过期数据
    _cutoff = _now_ts - _HISTORY_MAX_SECONDS
    while _VIX_HISTORY and _VIX_HISTORY[0][0] < _cutoff:
        _VIX_HISTORY.pop(0)
    while _SPY_HISTORY and _SPY_HISTORY[0][0] < _cutoff:
        _SPY_HISTORY.pop(0)
    # 限制最大条目数
    if len(_VIX_HISTORY) > _HISTORY_MAX_ENTRIES:
        _VIX_HISTORY[:] = _VIX_HISTORY[-_HISTORY_MAX_ENTRIES:]
    if len(_SPY_HISTORY) > _HISTORY_MAX_ENTRIES:
        _SPY_HISTORY[:] = _SPY_HISTORY[-_HISTORY_MAX_ENTRIES:]
    # 调用快速切换检测
    try:
        from app.strategy.regime_filter import check_rapid_regime_change
        _rapid = check_rapid_regime_change(
            vix_history=[v for _, v in _VIX_HISTORY],
            spy_history=[s for _, s in _SPY_HISTORY],
        )
        if _rapid:
            logger.warning(f"[RegimeQuickSwitch] 检测到快速切换 → {_rapid}, 本轮强制收紧")
            # 注入到 scheduler 供下游 signal_stage 读取
            scheduler._rapid_regime_override = _rapid
        else:
            scheduler._rapid_regime_override = None
    except Exception as _rre:
        logger.debug(f"[RegimeQuickSwitch] 检测异常 (不影响主流程): {_rre}")
        scheduler._rapid_regime_override = None

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

    # ── Task #3 (2026-06-17): 策略健康评估 (每小时一次) ──
    try:
        from app.risk.strategy_health_monitor import get_strategy_health_monitor
        get_strategy_health_monitor().evaluate_all_strategies()
    except Exception as _sh_err:
        logger.debug(f"[StrategyHealth] 评估调用失败 (不影响主流程): {_sh_err}")

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

        # ─── P0 修复 (2026-06-05): 禁用广度预扫描的数据获取 ───
        # 根因: get_full_market_context 在某些标的上无限阻塞(网络/rate limiter),
        #       导致整个预扫描循环卡住, 主循环永远不执行
        # 修复: 预扫描只做广度初始化, 数据获取由主循环逐symbol处理(有try/except保护)
        _context_cache = {}
        try:
            from app.analysis.market_breadth import get_market_breadth
            _breadth = get_market_breadth()
            _breadth.start_new_scan()
        except Exception:
            pass

        for symbol in batch:
            if is_circuit_breaker_open():
                logger.warning("[熔断] 熔断器开启，终止扫描")
                break
            try:
                # Self-managed 符号过滤: 用户手动管理的标的不进入信号管线 (yaml + DB)
                try:
                    from app.core.config_container import get_all_manual_symbols
                    _sm = get_all_manual_symbols()
                    # 兼容 US. 前缀
                    if symbol in _sm or symbol.replace("US.", "") in _sm:
                        logger.debug(f"[扫描] {symbol} 是 self_managed，跳过信号管线")
                        continue
                except Exception:
                    pass
                if scheduler._is_in_cooldown(symbol):
                    logger.debug(f"[扫描] {symbol} 处于冷却中，跳过")
                    continue
                if scheduler.position_store.has(symbol):
                    # P1 修复 (2026-06-19)
                    logger.debug(f"[扫描] {symbol} 已有持仓, 进入金字塔加仓检测")
                _process_symbol_new_pipeline(scheduler, symbol, vix, spy_context, market_news.get("sentiment", 0))
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

        # ─── P0 修复 (2026-06-05): 禁用广度预扫描数据获取 (CRA 路径) ───
        _context_cache_cra = {}
        try:
            from app.analysis.market_breadth import get_market_breadth
            _breadth_cra = get_market_breadth()
            _breadth_cra.start_new_scan()
        except Exception:
            pass

        # ─── 截面排名过滤 (2026-06-03) ───
        # 对 watchlist 做截面排名，只交易 Top N 标的
        # 原理：截面动量（个股 vs 所有其他股票）比时间序列动量（个股 vs 自己的历史）
        # 有更强的短期预测力 (Jegadeesh & Titman 1993)
        _ranked_watchlist = None
        try:
            from app.signals.pre_market_ranker import get_top_candidates
            # 从 config 读取排名参数
            from app.core.config_container import get_config
            _rank_cfg = get_config().get_section('pre_market_ranker', {}) or {}
            _rank_enabled = _rank_cfg.get('enabled', True)
            _rank_top_n = _rank_cfg.get('top_n', 8)
            
            if _rank_enabled:
                _ranked = get_top_candidates(list(scheduler.watchlist), max_count=_rank_top_n)
                if _ranked:
                    _ranked_watchlist = {r.symbol for r in _ranked}
                    logger.info(
                        f"[截面排名] 从 {len(scheduler.watchlist)} 只中选出 Top {_rank_top_n}: "
                        f"{[r.symbol.replace('US.','') for r in _ranked[:5]]}"
                    )
        except Exception as _rank_err:
            logger.debug(f"[截面排名] 排名器异常(降级为全量扫描): {_rank_err}")
        
        for symbol in scheduler.watchlist:
            if is_circuit_breaker_open():
                logger.warning("[熔断] 熔断器开启，终止扫描")
                break
            # self_managed 符号过滤: CRA fallback 路径也需要隔离 (P0 2026-06-11)
            try:
                from app.core.config_container import get_all_manual_symbols
                _sm = get_all_manual_symbols()
                if symbol in _sm or symbol.replace("US.", "") in _sm:
                    logger.debug(f"[扫描] {symbol} 是 self_managed，跳过信号管线 (CRA)")
                    continue
            except Exception:
                pass
            # 截面排名过滤: 只允许 Top N 进入信号生成
            if _ranked_watchlist is not None and symbol not in _ranked_watchlist:
                continue
            # 动态池过滤
            if pool_filter and not _passes_pool_filter(scheduler, symbol, pool_filter):
                logger.debug(f"[动态池·过滤] {symbol} 不满足当前池条件 (tier={pool_filter.get('tier')}, risk={pool_filter.get('risk_level')})")
                continue
            try:
                if scheduler._is_in_cooldown(symbol):
                    continue
                if scheduler.position_store.has(symbol):
                    # P1 修复 (2026-06-19)
                    # 已有持仓，跳过（进入金字塔加仓流程）
                    continue
                # P1 (2026-05-26): 新管线 CRA 模式 — 用 modular stages 生成候选,
                # 然后走 PortfolioConstructor 全局排序, 风控统一在 _submit_order 执行
                _cached_ctx = _context_cache_cra.get(symbol)  # H1: 复用广度预扫描缓存
                _candidate = _run_new_pipeline_to_candidate(
                    scheduler, symbol, vix, spy_context, market_news.get("sentiment", 0),
                    cached_context=_cached_ctx)
                if _candidate is not None:
                    _sig, _ctx, _vr = _bridge_candidate_to_old_format(_candidate)
                    _collect(symbol, _sig, _ctx, _vr)
                record_api_success()
            except Exception as e:
                record_api_error()
                logger.error(f"[扫描] {symbol} 处理异常：{e}", exc_info=True)

        # 组合构建: 评分 → 排序 → 分配 → 执行
        # 2026-06-26: TRADING + playbook独占时, 跳过下单 (playbook auto_execute负责)
        if candidates:
            if getattr(scheduler, '_skip_execution_in_trading', False):
                logger.info(f"[组合构建] {len(candidates)} 候选已生成, 但 TRADING 阶段 playbook 独占执行, 跳过下单")
            else:
                _execute_portfolio_plan(scheduler, candidates, vix)
        else:
            logger.info("[组合构建] 本轮无合格候选")


# ============================================================
#  P1 (2026-05-26): CRA 模式下的新管线集成
# ============================================================

def _run_new_pipeline_to_candidate(scheduler, symbol, vix, spy_context, market_sentiment, cached_context=None):
    """运行新管线 market_data + signal 阶段 (不含 risk), 返回 SignalCandidate

    CRA 模式专用: 只生成信号候选, 风控和仓位计算延迟到 _submit_order 统一执行。
    H1 (2026-06-03): cached_context 参数复用广度预扫描缓存
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
    
    # Phase 1: Shadow 信号记录 - 追踪所有生成的信号（包括未执行的）
    if candidate is not None:
        try:
            from app.storage.shadow_signal_logger import get_shadow_signal_logger
            shadow_logger = get_shadow_signal_logger()
            shadow_logger.record(
                candidate=candidate,
                context={
                    "price": ctx.price if ctx else None,
                    "volume": ctx.volume if ctx else None,
                    "vix": vix,
                    "regime": getattr(candidate, 'regime', None),
                    "market_sentiment": market_sentiment,
                }
            )
        except Exception as e:
            logger.warning(f"[ShadowSignal] 记录失败（不影响主流程）: {e}")
    
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
            # Preserve quality scoring and fusion metadata
            "quality_factors": candidate.metadata.get("quality_factors") if hasattr(candidate, "metadata") and candidate.metadata else None,
            "fused": candidate.metadata.get("fused") if hasattr(candidate, "metadata") and candidate.metadata else None,
            "fused_count": candidate.metadata.get("fused_count") if hasattr(candidate, "metadata") and candidate.metadata else None,
            "fused_strategies": candidate.metadata.get("fused_strategies") if hasattr(candidate, "metadata") and candidate.metadata else None,
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
    # 2026-07-25 修复 (handoff 07-24 深夜巡检 异常①): 启动预热 ImmediateScan/force_scan
    #   走本入口直调 _run_signal_scan_impl, 绕过了 scan_loop 内的主线静默门 →
    #   启动期跑了一轮 stages Pipeline (44 标的)。此处补同款 main_scan_enabled 门。
    #   回滚: 删本块。
    try:
        from app.core.config_container import get_config as _get_cfg_ms
        if not bool(_get_cfg_ms().get_dotted("strategy.main_scan_enabled", True)):
            logger.info("[扫描] 主线扫描已静默 (strategy.main_scan_enabled=false), 跳过外部入口扫描 (预热/force_scan)")
            return
    except Exception:
        pass
    _run_signal_scan_impl(scheduler)


def process_symbol(scheduler, symbol: str, vix: float, spy_context: Dict[str, Any], market_sentiment: float):
    """处理单只股票：数据获取 → 信号生成 → 风控 → 下单"""
    _process_symbol_new_pipeline(scheduler, symbol, vix, spy_context, market_sentiment)


# ============================================================
#  技术面信号生成
# ============================================================
def _generate_technical_signal(scheduler, context: Dict[str, Any]) -> Dict[str, Any]:
    """盘中纯技术面信号生成 — IntradayScorer v2.0 多因子引擎"""
    from app.core.intraday_scorer import get_intraday_scorer

    scorer = get_intraday_scorer()
    signal = scorer.score(context)
    result = signal.to_vote_result()

    # P0 (2026-06-29): 标记 strategy_id, 防止被 _record_decision_to_db 误标为 voting_llm
    result["strategy_id"] = "technical_intraday"

    result["position_suggestion"].update({
        "stop_loss_pct": signal.stop_loss_pct,
        "atr_used": context.get("atr", 0),
    })
    result["market_regime"] = signal.flags.get("regime_label", "NORMAL")

    # P1 (2026-06-24): _factor_detail 透传到 context, 供 _enrich_candidate → candidate.factor_detail → trade_records
    # 此前 _factor_detail 只在 result dict 里 (写 decisions.db), 未透传到执行链 → trade_records.factor_detail 恒 NULL (0/376)
    _fd = result.get("_factor_detail") or getattr(signal, "factor_detail", None)
    if _fd and isinstance(context, dict):
        context["_factor_detail"] = _fd

    # ─── 因子级投票分解 (2026-06-02: 反馈闭环修复) ───
    # 将黑盒 "intraday_scorer_v2" 单分数分解为各因子的独立投票
    # (trend / momentum / volume / orderflow / lifecycle)
    # 使 decisions.db.model_votes 记录因子级明细, 支撑后续的因子权重学习
    try:
        _factor_votes = signal.decompose_to_votes()
        if _factor_votes:
            result["model_scores"] = _factor_votes
    except Exception as _fd_e:
        logger.debug(f"[信号] 因子分解失败 (保留原始 model_scores): {_fd_e}")

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

    # ── Task #3 (2026-06-17): IC监控策略健康过滤 ──
    _ic_filtered_count = 0
    try:
        from app.risk.strategy_health_monitor import get_strategy_health_monitor
        _health_monitor = get_strategy_health_monitor()
    except Exception:
        _health_monitor = None

    for symbol, signal, context, vote_result in candidates:
        # 策略健康检查: SUSPENDED 策略的信号不进入组合构建
        if _health_monitor:
            _strat_id = (
                vote_result.get("strategy_id", "")
                or getattr(signal, "strategy_id", "")
                or "unknown"
            )
            _allowed, _reason = _health_monitor.should_allow_signal(_strat_id)
            if not _allowed:
                logger.info(
                    f"[IC_Monitor] 跳过 {symbol} 策略{_strat_id}: {_reason}")
                _ic_filtered_count += 1
                continue

        plan_dir = battle_plan_targets.get(symbol, "")
        pc.add_candidate(symbol, signal, context, vote_result,
                         battle_plan_direction=plan_dir)

    if _ic_filtered_count > 0:
        logger.info(
            f"[IC_Monitor] 本轮过滤 {_ic_filtered_count} 个候选 (策略健康降级)")

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
        # ── P0 (2026-06-19): signal_dedup — CRA 组合路径与 polling 路径对齐 ──
        try:
            from app.risk.signal_dedup import get_signal_deduplicator
            _direction = str(getattr(c.signal, 'direction', '')).upper()
            _strat_id = getattr(c.signal, 'strategy_id', '') or ''
            if get_signal_deduplicator().is_duplicate(c.symbol, _direction, _strat_id):
                logger.info(f"[组合执行] {c.symbol} signal_dedup: 重复信号, 跳过")
                continue
        except Exception as _dedup_err:
            logger.debug(f"[组合执行] {c.symbol} signal_dedup 检查异常 (放行): {_dedup_err}")
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
#  CRA 管线辅助函数
# ============================================================


def _build_cra_model_scores(candidate, ctx) -> list:
    """CRA 管线: 将技术指标信号分解为因子级 model_scores

    将 RSI/MACD/Bollinger/Supertrend 各指标作为独立因子投票,
    使 decisions.db.model_votes 记录各指标的独立信号。

    Args:
        candidate: SignalCandidate 对象
        ctx: SymbolContext 对象

    Returns:
        list of dict, 每个 dict 代表一个因子的投票
    """
    votes = []
    # 从 signal_tags 或 rationale 解析激活的指标
    signal_tags = getattr(candidate, 'signal_tags', []) or []
    rationale = getattr(candidate, 'rationale', '') or ''

    # 如果 signal_tags 为空, 从 rationale 解析 (e.g. "3/4 指标共振 | score=65")
    indicators_active = signal_tags if signal_tags else []
    if not indicators_active and '/' in rationale:
        # 尝试从 "X/Y 指标共振" 推断
        pass

    # 基础因子: 策略级投票 (始终包含)
    votes.append({
        "model_id": candidate.strategy_id or "cra_pipeline",
        "strength": candidate.strength,
        "confidence": candidate.confidence,
        "direction": candidate.direction,
        "weight": 1.0,
        "reasoning": f"CRA pipeline: {signal_tags or rationale}",
    })

    # 从 context 提取指标级信号, 构建因子分解投票
    if ctx:
        _indicator_configs = {
            "rsi": {"weight": 0.30},
            "macd": {"weight": 0.25},
            "bollinger": {"weight": 0.25},
            "vwap": {"weight": 0.20},
        }
        for ind_name, ind_cfg in _indicator_configs.items():
            ind_val = getattr(ctx, ind_name, None) if hasattr(ctx, ind_name) else ctx.get(ind_name) if isinstance(ctx, dict) else None
            if ind_val is not None and ind_val != 0:
                # 推断方向: RSI<30=LONG, RSI>70=SHORT; MACD>0=LONG, <0=SHORT; 等等
                _dir = candidate.direction
                _str = min(100, abs(int(float(ind_val) * 10))) if isinstance(ind_val, (int, float)) else candidate.strength
                votes.append({
                    "model_id": f"tech_{ind_name}",
                    "strength": _str,
                    "confidence": candidate.confidence * 0.8,  # 单因子置信度打折
                    "direction": _dir,
                    "weight": ind_cfg["weight"],
                    "reasoning": f"{ind_name}={ind_val}",
                })

    return votes


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
        "news_provider": getattr(scheduler, 'news_provider', None),
        # P0 (2026-06-17): 快速 Regime 切换覆盖 — 由 scan_loop 检测注入
        "_rapid_regime_override": getattr(scheduler, '_rapid_regime_override', None),
        # P0 (2026-06-29): TRADING阶段LLM空转修复 — Playbook独占时跳过LLM投票
        "_skip_execution_in_trading": getattr(scheduler, '_skip_execution_in_trading', False),
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

    # ── Degradation cap (迁移自 Legacy _build_signal_from_vote L1918-1928) ──
    # 降级信号置信度封顶 0.60 — voting_coordinator 标记 degraded 时生效
    _vote_result = (candidate.context.metadata if candidate.context else {}).get("vote_result", {}) or {}
    _is_degraded = bool(_vote_result.get("degraded"))
    if _is_degraded and signal.confidence > 0.60:
        logger.warning(
            f"[降级信号] {symbol} confidence {signal.confidence:.2f} → 0.60 "
            f"(level={_vote_result.get('degradation_level', 0)}, "
            f"reason={_vote_result.get('degradation_reason', '')})"
        )
        signal.confidence = 0.60
        signal.metadata["degraded"] = True
        signal.metadata["degradation_level"] = _vote_result.get("degradation_level", 0)
        signal.metadata["degradation_reason"] = _vote_result.get("degradation_reason", "")

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
        # ADR-007: Regime position_scale 传递到 order_executor
        _regime_scale = (getattr(candidate, 'metadata', {}) or {}).get("regime_position_scale")
        if _regime_scale is not None and 0 < _regime_scale < 1.0:
            context["position_scale_override"] = _regime_scale
            logger.info(f"[Regime] {symbol} 仓位缩放: ×{_regime_scale}")
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
        "model_scores": _build_cra_model_scores(candidate, ctx),
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

    # ── P0 (2026-06-19): signal_dedup — CRA路径与legacy路径对齐 ──
    try:
        from app.risk.signal_dedup import get_signal_deduplicator
        _direction = str(getattr(signal, 'direction', '')).upper()
        _strat_id = getattr(signal, 'strategy_id', '') or ''
        if get_signal_deduplicator().is_duplicate(symbol, _direction, _strat_id):
            logger.info(f"[CRA] {symbol} signal_dedup: 重复信号, 跳过")
            return
    except Exception as _dedup_err:
        logger.debug(f"[CRA] signal_dedup 检查异常 (放行): {_dedup_err}")

    # ── P0 (2026-05-04): 信号持久化到 signals 表 (迁移自 Legacy L1792-1822) ──
    try:
        from datetime import datetime as _dt
        from app.core.db_utils import db_connect
        from app.core.paths import get_trade_records_db
        try:
            from app.utils.timezone import now_et as _now_et
            _signal_date = _now_et().strftime("%Y-%m-%d")
        except Exception:
            _signal_date = _dt.now().strftime("%Y-%m-%d")
        with db_connect(get_trade_records_db()) as conn:
            conn.execute(
                "INSERT INTO signals "
                "(signal_date, symbol, direction, signal_strength, confidence, "
                " entry_price, stop_loss, take_profit, strategy, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _signal_date,
                    symbol,
                    str(signal.direction),
                    signal.strength,
                    signal.confidence,
                    context.get("price", 0.0),
                    voting_result.get("position_suggestion", {}).get("stop_loss_pct", 0),
                    voting_result.get("position_suggestion", {}).get("take_profit_pct", 0),
                    signal.strategy_id or "voting_llm",
                    "PENDING",
                ),
            )
        logger.info(f"[P0·信号持久化] {symbol} 已写入 signals 表: strength={signal.strength} confidence={signal.confidence:.2f}")
    except Exception as e:
        logger.error(f"[P0·信号持久化] {symbol} 写入 signals 表失败: {e}")

    # ── 委托给现有执行路径 (PDT → Gates → Pyramid → Execute) ──
    # 2026-06-26: TRADING + playbook独占时, 跳过下单 (同 _execute_portfolio_plan 逻辑)
    if getattr(scheduler, '_skip_execution_in_trading', False):
        logger.info(f"[CRA] {symbol} 信号已生成, 但 TRADING 阶段 playbook 独占执行, 跳过下单")
        return

    from app.pipeline.order_executor import _submit_order
    lock = getattr(scheduler, '_open_position_lock', None)
    if lock:
        lock.acquire()
    try:
        _submit_order(scheduler, symbol, signal, context, voting_result)
    finally:
        if lock:
            lock.release()
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
            # M8 (2026-06-03): 使用 Push 缓存而非 HTTP 调用, 避免逐标的阻塞
            _futu = getattr(scheduler.market_data, 'futu', None) if scheduler.market_data else None
            if _futu and hasattr(_futu, '_push_quote_cache'):
                with _futu._push_cache_lock:
                    _cached = _futu._push_quote_cache.get(symbol, {})
                    # 2026-06-25 修复: 优先使用 bid/ask 中间价
                    _bid = float(_cached.get("bid", 0) or 0)
                    _ask = float(_cached.get("ask", 0) or 0)
                    if _bid > 0 and _ask > 0:
                        price = round((_bid + _ask) / 2.0, 2)
                    else:
                        price = float(_cached.get("price", 0) or _cached.get("last", 0) or 0)
            elif scheduler.market_data:
                quote = scheduler.market_data.get_quote(symbol)
                if quote:
                    _bid = float(quote.get("bid", 0) or 0)
                    _ask = float(quote.get("ask", 0) or 0)
                    if _bid > 0 and _ask > 0:
                        price = round((_bid + _ask) / 2.0, 2)
                    else:
                        price = quote.get("price", 0) or quote.get("last_price", 0)
            else:
                price = 0
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


