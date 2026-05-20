"""
FTSeek-US 核心调度器 - 触发器

包含:
- 市场时段判断 (交易中/盘前/盘后夜盘/周末休市)
- 信号冷却管理
- 每日复盘触发
- 每周复盘触发
- 每月复盘触发
"""
import logging
import os
from datetime import datetime, date, timedelta
from typing import TYPE_CHECKING
from enum import Enum
from app.core.paths import get_decisions_db, get_project_root

import pytz

if TYPE_CHECKING:
    from .scheduler_core import TradingScheduler

logger = logging.getLogger(__name__)

# 美东时区
ET = pytz.timezone("America/New_York")



def _get_contract_multiplier(symbol: str) -> int:
    """获取合约乘数: 美股期权=100, 股票=1"""
    parts = symbol.split(".")
    if len(parts) >= 2:
        ticker_part = parts[-1]
        if len(ticker_part) >= 15:
            return 100 if ("P" in ticker_part[6:10] or "C" in ticker_part[6:10]) else 1
    return 1


class MarketPhase(Enum):
    """市场运行阶段"""
    TRADING = "trading"           # 交易中 (09:30-16:00 ET, 工作日)
    PRE_MARKET = "pre_market"     # 盘前 (04:00-09:30 ET, 工作日)
    AFTER_HOURS = "after_hours"   # 盘后夜盘 (16:00-20:00 ET, 工作日)
    OVERNIGHT = "overnight"       # 深夜休市 (20:00-04:00 ET, 工作日)
    WEEKEND = "weekend"           # 周末/假日 (全天)


def get_market_phase() -> MarketPhase:
    """
    获取当前市场运行阶段

    时段划分 (美东时间):
    - 周末/假日 → WEEKEND (完全休眠，不请求任何 API)
    - 工作日 04:00-09:30 → PRE_MARKET (关注新闻，不轮询行情)
    - 工作日 09:30-16:00 → TRADING (完整轮询 + 策略执行)
    - 工作日 16:00-20:00 → AFTER_HOURS (关注新闻 + LLM 推荐)
    - 工作日 20:00-04:00 → OVERNIGHT (深度休眠，仅最低频率新闻)

    P0-D1 (2026-05-14): 接入 holiday_calendar — 节假日 (含半日市) 当作 WEEKEND
    """
    now_et = datetime.now(ET)
    weekday = now_et.weekday()  # 0=周一, 6=周日

    # 周末
    if weekday >= 5:
        return MarketPhase.WEEKEND

    # P0-D1: 节假日 (Memorial Day / July 4 / Thanksgiving / Christmas / etc.)
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        if get_holiday_calendar().is_us_holiday(now_et.date()):
            return MarketPhase.WEEKEND
    except Exception:
        pass  # 日历加载失败不阻断, 退化为仅周末判断

    hour = now_et.hour
    minute = now_et.minute

    # 盘中: 09:30 - 16:00
    if (hour > 9 or (hour == 9 and minute >= 30)) and hour < 16:
        return MarketPhase.TRADING

    # 盘前: 04:00 - 09:30
    if 4 <= hour < 9 or (hour == 9 and minute < 30):
        return MarketPhase.PRE_MARKET

    # 盘后夜盘: 16:00 - 20:00
    if 16 <= hour < 20:
        return MarketPhase.AFTER_HOURS

    # 深夜休市: 20:00 - 04:00
    return MarketPhase.OVERNIGHT


def is_market_open() -> bool:
    """
    判断美东交易时间（09:30 - 16:00 周一到周五）

    Returns:
        bool: 是否在交易时间内
    """
    return get_market_phase() == MarketPhase.TRADING


def is_in_cooldown(scheduler: 'TradingScheduler', symbol: str) -> bool:
    """
    检查信号冷却

    P1 修复 (2026-05-04): 使用 scheduler._open_position_lock 保护，
    防止 signal_pipeline 和 force_scan 线程并发读写 _signal_cooldown 导致竞态。
    """
    with scheduler._open_position_lock:
        if symbol not in scheduler._signal_cooldown:
            return False
        elapsed = (datetime.now() - scheduler._signal_cooldown[symbol]).total_seconds() / 60
        return elapsed < scheduler.SIGNAL_COOLDOWN_MINUTES


def set_cooldown(scheduler: 'TradingScheduler', symbol: str):
    """
    设置信号冷却

    P1 修复 (2026-05-04): 使用 scheduler._open_position_lock 保护写入。
    """
    with scheduler._open_position_lock:
        scheduler._signal_cooldown[symbol] = datetime.now()


def _trigger_eod_agent_review(scheduler, review: dict):
    """
    通过 Agent 桥接触发盘后全链路归因分析
    
    收集全天数据（交易、信号、拒绝、资金轨迹）写入 Agent 请求
    修复 2026-04-25: 传递真实决策数据 + 消费 Agent 响应更新记忆
    """
    try:
        bridge = scheduler.agent_bridge
        
        # 收集全天交易
        trades = []
        try:
            from app.storage.trade_store import TradeStore
            db = str(get_project_root() / "data" / "ftseek_us.db")
            ts = TradeStore(db)
            # P0 修复 (2026-05-02): 使用ET日期查询DB，避免UTC日期在00:00-04:00 ET窗口与trade_date错位
            # EOD在16:00+ ET触发，此时ET日期=交易日期，trade_date与之一致
            today_str = datetime.now(ET).date().isoformat()
            trades = ts.query_by_date(today_str)
        except Exception as e:
            logger.warning(f"[EOD Review] trades 数据收集失败: {e}")
            pass
        
        # 收集真实决策漏斗数据
        signal_data = {"total_decisions": 0, "by_direction": {}}
        try:
            import sqlite3
            db_path = str(get_decisions_db())
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                today_str = datetime.now(ET).date().isoformat()
                total = conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE date(created_at) = ?",
                    (today_str,)
                ).fetchone()[0]
                signal_data["total_decisions"] = total
                
                by_dir = {}
                rows = conn.execute(
                    "SELECT direction, final_strength, COUNT(*) as cnt FROM decisions "
                    "WHERE date(created_at) = ? GROUP BY direction, final_strength",
                    (today_str,)
                ).fetchall()
                for r in rows:
                    by_dir[f"{r[0]}(s={r[1]})"] = r[2]
                signal_data["by_direction"] = by_dir
                conn.close()
        except Exception as e:
            logging.getLogger("ftseek_us.agent_bridge").debug(f"收集决策数据失败: {e}")
        
        # 拒绝统计
        rejections = {}
        try:
            from app.storage.rejection_logger import get_rejection_logger
            rl = get_rejection_logger()
            rejections = rl.get_today_summary()
        except Exception as e:
            logger.warning(f"[EOD Review] 拒绝统计收集失败: {e}")
            pass
        
        # 资金轨迹
        capital = {}
        try:
            from app.risk.capital_safety import get_capital_safety_system
            cs = get_capital_safety_system()
            if cs._state:
                capital = {
                    "start_equity": getattr(cs._state, 'initial_capital', 0),
                    "end_equity": getattr(cs._state, 'current_equity', 0),
                    "max_drawdown": getattr(cs._state, 'max_drawdown_pct', 0),
                }
        except Exception as e:
            logger.warning(f"[EOD Review] 资金轨迹收集失败: {e}")
            pass
        
        # 作战计划
        battle_plan = {}
        try:
            from app.us_pipeline.battle_plan_manager import USBattlePlanManager
            mgr = USBattlePlanManager()
            battle_plan = mgr.get_plan() or {}
        except Exception as e:
            logger.warning(f"[EOD Review] 作战计划加载失败: {e}")
            pass
        try:
            search_terms = []
            for t in trades:
                search_terms.append(t.get("symbol", ""))
                search_terms.append(t.get("exit_reason", ""))
            query = " ".join(search_terms[:10])
            if query.strip():
                bridge.search_lessons(query, top_k=3)
        except Exception:
            pass

        # 写入 Agent 请求
        request = bridge.generate_eod_review(
            trades=trades,
            signals=signal_data,
            rejections=rejections,
            capital=capital,
            battle_plan=battle_plan
        )
        
        logging.getLogger("ftseek_us.agent_bridge").info(
            f"[Agent] EOD 归因请求已写入 | trades={len(trades)} | "
            f"decisions={signal_data['total_decisions']} | "
            f"rejections={rejections.get('total', 0)}"
        )
        
        # 消费 Agent 响应 (更新记忆)
        # P1 修复 (2026-05-03): 异步消费，不再阻塞 scheduler 主线程 600s
        def _consume_eod_async():
            try:
                eod_review = bridge._check_eod_response(request_id=request['id'], poll_seconds=600)
                if eod_review:
                    logging.getLogger("ftseek_us.agent_bridge").info(
                        f"[Agent] EOD 归因响应已消费 | lessons={len(eod_review.get('lessons', []))}"
                    )
            except Exception as e:
                logging.getLogger("ftseek_us.agent_bridge").debug(f"消费 EOD 响应失败: {e}")
        
        import threading
        threading.Thread(target=_consume_eod_async, daemon=True, name="eod-response-consumer").start()
            
    except Exception as e:
        import logging
        logging.getLogger("ftseek_us.agent_bridge").debug(
            f"[Agent] EOD 归因触发异常: {e}"
        )


def check_daily_review(scheduler: 'TradingScheduler'):
    """
    每日 16:00+ ET 触发复盘推送 (放宽窗口，避免 10 分钟扫描间隔错过 16:05)
    
    2026-04-24 修复: 原逻辑要求 exactly minute=5，但扫描间隔 10 分钟，
    如果扫描在 16:00 运行，下次是 16:10，16:05 被永久错过。
    改为: hour >= 16 且日期未执行过 → 触发。
    
    2026-05-02 升级: 集成 review_loop 改进闭环 (IssueDetector + ActionExecutor)
    
    Args:
        scheduler: TradingScheduler 实例
    """
    now_et = datetime.now(ET)
    # 放宽条件: 16:00 之后任何时间都可以触发 (只要当天未执行过)
    if now_et.hour >= 16:
        if getattr(scheduler, '_last_review_date', None) != now_et.date():
            scheduler._last_review_date = now_et.date()
            try:
                from app.review.daily_review import generate_daily_review
                review = generate_daily_review(now_et.date().isoformat())
                if scheduler.notifier:
                    scheduler.notifier.send_text(_format_daily_review(report=review))
                logger.info(f"[日报] 复盘推送完成：{now_et.date()}")
                
                # P3 (2026-05-03): Review Orchestrator — 辩论增强复盘 + LLM Issue 注入
                # 2026-05-05 升级: 移到 run_review_loop 之前, 将 LLM 辩论提取的 Issue 注入 review dict
                # 使 IssueDetector 能检测 LLM 提取的 Issue, ActionExecutor 能自动修复
                try:
                    from app.review.review_orchestrator import get_review_orchestrator
                    orchestrator = get_review_orchestrator(
                        notifier=scheduler.notifier,
                        agent_bridge=scheduler.agent_bridge,
                    )
                    orch_result = orchestrator.run_daily(now_et.date().isoformat())
                    
                    # 注入 LLM 辩论提取的 Issue 到 review dict
                    llm_issues = orch_result.get("llm_issues", [])
                    if llm_issues:
                        review["llm_issues"] = llm_issues
                        logger.info(f"[Orchestrator] 注入 {len(llm_issues)} 个 LLM Issue 到 review")
                    
                    logger.info(
                        f"[Orchestrator] 日复盘增强完成: "
                        f"mode={orch_result.get('analysis_mode')} | "
                        f"lessons={orch_result.get('lessons_extracted', 0)} | "
                        f"llm_issues={len(llm_issues)}"
                    )
                except Exception as oe:
                    logger.debug(f"[Orchestrator] 日复盘增强失败 (不影响主流程): {oe}")
                # ── LLM Issue 注入结束 ──
                # ── 2026-05-02: 新增改进闭环 ──
                try:
                    from app.core.review_loop import run_review_loop
                    loop_context = {}
                    # 从 config 获取当前参数作为修复基准
                    try:
                        from app.core.config_container import get_config
                        cfg = get_config()
                        risk_cfg = cfg.get("risk", {})
                        loop_context["MIN_CONFIDENCE"] = risk_cfg.get("MIN_CONFIDENCE", 0.50)
                        loop_context["SIGNAL_MIN_SCORE"] = risk_cfg.get("SIGNAL_MIN_SCORE", 50)
                        loop_context["MAX_POSITION_PER_SYMBOL"] = risk_cfg.get("MAX_POSITION_PER_SYMBOL", 0.25)
                    except Exception as e:
                        logger.debug(f"[ReviewLoop] 配置加载失败，使用默认值: {e}")
                        pass
                    
                    loop_result = run_review_loop(review, notifier=scheduler.notifier, context=loop_context)
                    logger.info(f"[ReviewLoop] 改进闭环完成: {loop_result['issues_detected']} 问题, {loop_result['actions_taken']} 修复")
                    
                    # P0 修复 (2026-05-09): 仅在日复盘时发送次日提示，避免周报/盘中复盘重复
                    exp = loop_result.get("experience_result", {})
                    if scheduler.notifier and exp.get("lesson_count", 0) > 0:
                        hints = exp.get("next_day_hints", {})
                        hint_msg = f"📋 次日交易提示 ({exp['lesson_count']} 条活跃教训)\n"
                        if hints.get("top_risk"):
                            hint_msg += f"\n⚠️ {hints['top_risk']}"
                        if hints.get("avoid_sessions"):
                            hint_msg += f"\n🚫 避免时段: {', '.join(hints['avoid_sessions'])}"
                        if hints.get("caution_symbols"):
                            hint_msg += f"\n🔍 谨慎标的: {', '.join(hints['caution_symbols'][:5])}"
                        scheduler.notifier.send_text(hint_msg)
                except Exception as loop_err:
                    logger.debug(f"[ReviewLoop] 改进闭环失败 (不影响主流程): {loop_err}")
                # ── 改进闭环结束 ──
                

                
                # P0-fix 2026-05-02: 教训验证 — 检查活跃教训的有效性并自动调整置信度
                try:
                    from app.review.lesson_injector import LessonInjector
                    li = getattr(scheduler, 'lesson_injector', None)
                    if li is None:
                        li = LessonInjector()
                    # 获取今日交易记录用于验证
                    today_trades = review.get('summary', {}).get('trades', [])
                    validation_result = li.validate_lessons(today_trades)
                    logger.info(
                        f"[教训验证] 验证完成: "
                        f"有效={validation_result.get('validated', 0)}, "
                        f"无效={validation_result.get('invalidated', 0)}, "
                        f"更新={validation_result.get('updated', 0)}"
                    )
                    if scheduler.notifier and validation_result.get('updated', 0) > 0:
                        scheduler.notifier.send_text(
                            f"📋 [教训验证] 今日更新 {validation_result['updated']} 条教训: "
                            f"✅ 验证 {validation_result.get('validated', 0)} 条, "
                            f"❌ 失效 {validation_result.get('invalidated', 0)} 条"
                        )
                except Exception as ve:
                    logger.debug(f"[教训验证] 失败 (不影响主流程): {ve}")
                
                # ── LLM 逐笔归因 (角色重定位 2026-05-13) ──
                # 对每笔已平仓交易调 LLM 做归因，输出写入 trade_attribution
                try:
                    from app.llm.attribution_engine import get_attribution_engine
                    from app.storage.trade_recorder import get_trade_recorder
                    
                    engine = get_attribution_engine()
                    recorder = get_trade_recorder()
                    
                    # 获取今日已平仓交易
                    today_str = now_et.date().isoformat()
                    today_trades = recorder.get_trades_by_date(today_str) if hasattr(recorder, 'get_trades_by_date') else []
                    
                    for trade in today_trades:
                        if trade.get("exit_time"):  # 只归因已平仓
                            attribution = engine.attribute_trade(trade, market_context={
                                "vix": review.get("market_context", {}).get("vix", "N/A"),
                                "spy_chg": review.get("market_context", {}).get("spy_change", 0),
                                "session": "after_hours",
                                "regime": review.get("market_context", {}).get("regime", "N/A"),
                            })
                            engine.save(attribution)
                    
                    if today_trades:
                        logger.info(f"[LLM归因] 今日 {len(today_trades)} 笔交易归因完成")
                except Exception as lae:
                    logger.debug(f"[LLM归因] 失败 (不影响主流程): {lae}")
                # ── LLM 逐笔归因结束 ──
                
                # Phase 5: 通过 Agent 桥接触发盘后全链路归因
                if hasattr(scheduler, 'agent_bridge') and scheduler.agent_bridge:
                    try:
                        _trigger_eod_agent_review(scheduler, review)
                    except Exception as ae:
                        logger.debug(f"[Agent] 触发盘后归因失败 (不影响主流程): {ae}")
                
                # Phase 6: 影子策略测试评估 (按策略线分别评估)
                try:
                    from app.evolution.shadow_tracker import ShadowTracker
                    
                    tracker = ShadowTracker()
                    target_symbol = "SPY"
                    date_str = now_et.date().isoformat()
                    
                    # 遍历主线和 Scalping 两条策略线
                    for strategy_type in ["main", "scalping"]:
                        results = tracker.run_daily_eval(strategy_type, target_symbol, date_str)
                        
                        for res in results:
                            if res['decision'] == 'promoted':
                                if scheduler.notifier:
                                    scheduler.notifier.send_text(
                                        f"🚀 [自动优化] {strategy_type.upper()} 策略影子胜出!\\n"
                                        f"提议: {res['proposal_id']}\\n"
                                        f"参数已自动更新并生效。"
                                    )
                                logger.info(f"✅ [Shadow] {strategy_type} 提议 {res['proposal_id']} 胜出，自动转正!")
                except Exception as ste:
                    logger.debug(f"[Shadow] 影子测试评估失败 (不影响主流程): {ste}")
                

            except Exception as e:
                logger.error(f"[日报] 复盘推送失败：{e}", exc_info=True)


def check_weekly_review(scheduler: 'TradingScheduler'):
    """
    每周五 16:00+ ET 触发周报推送
    2026-04-24 修复: 放宽窗口，避免 10 分钟扫描间隔错过 16:10
    2026-05-02 修复: 移除对不存在模块 (AutoTuner, decision_recorder) 的引用，
                    接入完整 review_loop 闭环，消除周报重复生成
    """
    now_et = datetime.now(ET)
    # 周五 (weekday=4) 16:00+ ET
    if now_et.weekday() == 4 and now_et.hour >= 16:
        week_start = (now_et - timedelta(days=now_et.weekday())).strftime('%Y-%m-%d')
        week_end = now_et.strftime('%Y-%m-%d')

        if not hasattr(scheduler, '_last_weekly_review'):
            scheduler._last_weekly_review = None

        if scheduler._last_weekly_review != week_end:
            scheduler._last_weekly_review = week_end
            try:
                from app.review.weekly_review import generate_weekly_review
                report = generate_weekly_review(week_start, week_end)
                if scheduler.notifier:
                    scheduler.notifier.send_text(_format_weekly_review(report=report))
                logger.info(f"[周报] 复盘推送完成：{week_start} 至 {week_end}")
                
                # 2026-05-02: 接入完整 review_loop 闭环 (替代已废弃的 AutoTuner)
                try:
                    from app.core.review_loop import run_review_loop
                    loop_result = run_review_loop(report, notifier=scheduler.notifier)
                    
                    # 周度特有问题检测
                    p0_count = len([
                        a for a in loop_result.get("actions", [])
                        if "P0" in str(a.get("details", ""))
                    ])
                    if p0_count > 0:
                        logger.warning(f"[WeeklyReview] 检测到 {p0_count} 个 P0 问题")
                    
                    logger.info(
                        f"[ReviewLoop] 周度闭环完成: {loop_result['issues_detected']} 问题, "
                        f"{loop_result['actions_taken']} 修复"
                    )
                except Exception as loop_err:
                    logger.debug(f"[ReviewLoop] 周度闭环失败 (不影响主流程): {loop_err}")
                
                # P3 (2026-05-03): Review Orchestrator 周度增强
                try:
                    from app.review.review_orchestrator import get_review_orchestrator
                    orchestrator = get_review_orchestrator(
                        notifier=scheduler.notifier,
                        agent_bridge=scheduler.agent_bridge,
                    )
                    orch_result = orchestrator.run_weekly(week_start, week_end)
                    logger.info(
                        f"[Orchestrator] 周复盘增强完成: "
                        f"mode={orch_result.get('analysis_mode')} | "
                        f"trades={orch_result.get('trades', 0)}"
                    )
                except Exception as oe:
                    logger.debug(f"[Orchestrator] 周复盘增强失败 (不影响主流程): {oe}")
            except Exception as e:
                logger.error(f"[周报] 复盘推送失败：{e}", exc_info=True)
            
            # ── LLM 周度假设生成 (角色重定位 2026-05-13) ──
            # 每周五盘后调 LLM 生成 1-3 个参数调整提议
            if scheduler._last_weekly_review == week_end:  # 只在周报复盘成功后触发
                try:
                    from app.llm.weekly_proposer import get_weekly_proposer
                    from app.evolution.improvement_pipeline import get_pipeline
                    from app.llm.multi_model_client import get_multi_model_client
                    
                    mmc = get_multi_model_client()
                    if mmc.is_budget_exhausted():
                        logger.warning("[周度假设] 日预算已用完，跳过")
                    else:
                        proposer = get_weekly_proposer(llm_client=mmc, model="deepseek")
                        proposals = proposer.generate_weekly_proposals(days=7)
                        
                        if proposals:
                            pipeline = get_pipeline()
                            for p in proposals:
                                pid = pipeline.submit(p)
                                logger.info(
                                    f"[周度假设] 提议已提交: {pid} | "
                                    f"{p.target_param} {p.current_value} → {p.proposed_value}"
                                )
                            
                            if scheduler.notifier:
                                summary = "\n".join(
                                    f"• {p.target_param}: {p.current_value} → {p.proposed_value} ({p.rationale[:50]})"
                                    for p in proposals
                                )
                                scheduler.notifier.send_text(
                                    f"📊 [周度假设] 本周生成 {len(proposals)} 个参数调整提议:\n{summary}"
                                )
                        else:
                            logger.info("[周度假设] 本周交易不足或无有效提议")
                except Exception as wpe:
                    logger.debug(f"[周度假设] 失败 (不影响主流程): {wpe}")
            # ── LLM 周度假设生成结束 ──


def check_monthly_review(scheduler: 'TradingScheduler'):
    """
    每月 1 日 10:00+ ET 触发月报推送
    2026-04-24: 原逻辑要求 minute==0，易错过扫描间隔
    2026-05-02: 放宽触发条件 (hour>=10)，接入 review_loop 闭环
    """
    now_et = datetime.now(ET)
    # 放宽条件: 1 日 10:00+ ET
    if now_et.day == 1 and now_et.hour >= 10:
        # 计算上个月
        if now_et.month == 1:
            year = now_et.year - 1
            month = 12
        else:
            year = now_et.year
            month = now_et.month - 1
        
        review_key = f"{year}-{month:02d}"
        
        if not hasattr(scheduler, '_last_monthly_review'):
            scheduler._last_monthly_review = None
        
        if scheduler._last_monthly_review != review_key:
            scheduler._last_monthly_review = review_key
            try:
                from app.review.monthly_review import generate_monthly_review
                report = generate_monthly_review(year, month)
                if scheduler.notifier:
                    scheduler.notifier.send_text(_format_monthly_review(report=report))
                logger.info(f"[月报] 复盘推送完成：{year}年{month}月")
                
                # 2026-05-02: 接入 review_loop 闭环
                try:
                    from app.core.review_loop import run_review_loop
                    loop_result = run_review_loop(report, notifier=scheduler.notifier)
                    logger.info(
                        f"[ReviewLoop] 月度闭环完成: {loop_result['issues_detected']} 问题, "
                        f"{loop_result['actions_taken']} 修复"
                    )
                except Exception as loop_err:
                    logger.debug(f"[ReviewLoop] 月度闭环失败 (不影响主流程): {loop_err}")
            except Exception as e:
                logger.error(f"[月报] 复盘推送失败：{e}", exc_info=True)


def check_position_snapshot(scheduler: 'TradingScheduler'):
    """
    持仓快照任务
    
    - 每小时整点：hourly 快照
    - 15:55 ET：pre_close 快照
    - 16:05 ET：post_close 快照
    
    P0 修复 (2026-04-25): 从行情源实时获取 current_price,
    之前用 getattr(pos, 'current_price', 0) 始终返回 0.
    
    P0 修复 (2026-04-25): 非交易时间完全跳过，不调用行情API。
    """
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    phase = get_market_phase()
    
    # 优化 (2026-04-27): 盘前阶段也跳过快照，避免触发无效的 API 请求
    # Pre-Market 期间行情数据稀薄且不可靠，且不需要高频更新 PnL
    if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT, MarketPhase.PRE_MARKET):
        return
    
    now_et = datetime.now(ET)
    
    # 只有在有持仓时才记录
    if scheduler.position_store.count() == 0:
        return
    
    try:
        from app.storage.position_snapshot import get_position_snapshotter

        snapshotter = get_position_snapshotter()
        
        # 获取当前持仓信息
        positions = []
        for pos in scheduler.position_store.all():
            # P0 修复: 实时获取当前价格 (Position 对象没有 current_price 属性)
            current_price = 0.0
            try:
                if hasattr(scheduler, 'data_router') and scheduler.data_router:
                    ctx = scheduler.data_router.get_full_market_context(pos.symbol)
                    if ctx and ctx.get("price", 0) > 0:
                        current_price = ctx["price"]
            except Exception as e:
                logger.debug(f"[快照] {pos.symbol} 价格获取失败: {e}")
            
            # 如果有实时价格，计算盈亏
            if current_price > 0 and pos.avg_entry_price > 0:
                multiplier = _get_contract_multiplier(pos.symbol)
                if pos.direction.value == "LONG":
                    unrealized_pnl = (current_price - pos.avg_entry_price) * pos.size * multiplier
                    pnl_pct = (current_price - pos.avg_entry_price) / pos.avg_entry_price * 100
                else:
                    unrealized_pnl = (pos.avg_entry_price - current_price) * pos.size * multiplier
                    pnl_pct = (pos.avg_entry_price - current_price) / pos.avg_entry_price * 100
                market_value = pos.size * current_price
            else:
                unrealized_pnl = 0
                pnl_pct = 0
                market_value = 0
            
            meta = getattr(pos, "metadata", {}) or {}
            positions.append({
                "symbol": pos.symbol,
                "direction": pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction),
                "quantity": pos.size,
                "avg_cost": pos.avg_entry_price,
                "current_price": current_price,
                "market_value": market_value,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
                "stop_loss": pos.current_sl,
                "take_profit": pos.take_profit_levels[0]['price'] if pos.take_profit_levels else 0,
                "signal_strength": meta.get("signal_strength", 0),
                "signal_confidence": meta.get("signal_confidence", 0),
                "holding_hours": pos.holding_hours,
            })
        
        # 每小时整点记录
        if now_et.minute == 0:
            snapshotter.take_snapshot(positions, "hourly")
        
        # 收盘前快照 (15:55 ET)
        if now_et.hour == 15 and now_et.minute == 55:
            snapshotter.take_snapshot(positions, "pre_close")
        
        # 收盘后快照 (16:05 ET)
        if now_et.hour == 16 and now_et.minute == 5:
            snapshotter.take_snapshot(positions, "post_close")
            
    except Exception as e:
        logger.error(f"[快照] 记录失败：{e}", exc_info=True)


def _format_daily_review(report: dict) -> str:
    """格式化日报为文本 (v2.0: 综合评分 + 6维度 + 市场状态)"""
    date_str = report.get('report_date', '')
    summary = report.get('summary', {})
    version = report.get('report_version', '1.0')
    
    total = summary.get('total_trades') or 0
    closed = summary.get('closed_trades') or 0
    wins = summary.get('winning_trades') or 0
    pnl = summary.get('total_pnl') or 0
    win_rate = summary.get('win_rate') or 0
    
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    
    # ── v2.0: 综合评分 ──
    score_data = report.get('review_score', {})
    score = score_data.get('overall_score')
    score_status = score_data.get('status', 'unknown')
    score_text = ""
    if score is not None:
        score_emoji = "🟢" if score >= 7 else "🟡" if score >= 5 else "🔴"
        status_map = {'excellent': '优秀', 'good': '良好', 'needs_improvement': '需改进', 'critical': '警告', 'no_trades': '无交易'}
        score_text = f"\n{score_emoji} 今日评分: {score:.1f}/10 ({status_map.get(score_status, score_status)})"
        # 6维度简要
        dims = report.get('dimensions', {})
        if dims:
            score_text += "\n  📊 6维度:"
            dim_labels = {
                'signal_quality': '信号质量', 'voting_consistency': '投票一致性',
                'execution_quality': '执行质量', 'risk_compliance': '风控合规',
                'funnel_diagnosis': '漏斗诊断', 'attribution_learning': '归因学习',
            }
            for key, label in dim_labels.items():
                dim = dims.get(key, {})
                if key == 'risk_compliance':
                    val = dim.get('risk_score')
                    score_text += f"\n    {label}: {val:.1f}/10" if val else f"\n    {label}: N/A"
                elif key == 'signal_quality':
                    assessment = dim.get('assessment', '')
                    score_text += f"\n    {label}: {assessment}" if assessment else f"\n    {label}: N/A"
                else:
                    status = dim.get('status', dim.get('data_available', ''))
                    if isinstance(status, bool):
                        status = '✅' if status else '❌'
                    elif status:
                        status = str(status)
                    else:
                        status = 'N/A'
                    score_text += f"\n    {label}: {status}"
    
    # ── v2.0: 市场状态 ──
    regime = report.get('market_regime', {})
    regime_text = ""
    regime_state = regime.get('state', 'unknown')
    if regime_state and regime_state != 'unknown':
        regime_text = f"\n🌍 市场状态: {regime_state} ({regime.get('confidence', 'low')})"
    
    # 信号漏斗
    funnel = report.get('signal_funnel', {})
    funnel_text = f"• 决策数: {funnel.get('total_decisions', 0)} 条"
    
    # 拒绝分析
    rejections = report.get('rejection_analysis') or {}
    rej_text = ""
    if (rejections.get('total') or 0) > 0:
        top = rejections.get('top_rejector', 'unknown')
        rej_text = f"\n🚫 拒绝分析\n• 总拒绝: {rejections['total']} 次\n• TOP 原因: {top}"
        by_reason = rejections.get('by_reason', {})
        if by_reason:
            rej_text += "\n• 分类: " + ", ".join([f"{k}:{v}" for k, v in list(by_reason.items())[:3]])
    
    # 资金状态
    capital = report.get('capital_state', {})
    cap_text = ""
    if capital and 'current_equity' in capital:
        cap_text = (
            f"\n💰 资金状态\n"
            f"• 净值: ${capital['current_equity']:,.0f}\n"
            f"• 现金: ${capital['cash']:,.0f}\n"
            f"• 回撤: {capital['drawdown_pct']:.2f}%\n"
            f"• 水位: {capital['water_level']}"
        )
    
    # v2.0: 计划对比
    plan_text = ""
    pva = report.get('plan_vs_actual', {})
    if pva.get('plan_available'):
        executed = len(pva.get('executed_targets', []))
        missed = len(pva.get('missed_opportunities', []))
        unplanned = len(pva.get('unplanned_trades', []))
        plan_text = (
            f"\n📋 计划执行\n"
            f"• 已执行: {executed} | 错过: {missed} | 计划外: {unplanned}"
        )
    
    # Phase 5: 逐笔交易分析摘要 (2026-05-02 新增)
    trade_analyses = report.get('trade_analyses', [])
    verdict_text = ""
    if trade_analyses:
        verdict_counts = {}
        for ta in trade_analyses:
            v = ta.get('verdict', '未知')
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        
        verdict_parts = [f"{v}: {c}笔" for v, c in verdict_counts.items()]
        verdict_text = f"\n🔍 交易判定\n• {' | '.join(verdict_parts)}"
        
        # 显示需要辩论的交易
        debate_trades = [ta for ta in trade_analyses if ta.get('needs_debate')]
        if debate_trades:
            verdict_text += f"\n⚡ 待辩论: {len(debate_trades)}笔"
            for dt in debate_trades[:2]:
                verdict_text += f"\n  • #{dt['trade_id']} {dt['symbol']} ({dt['verdict']}): {dt['debate_reason'][:40]}"
    
    # 改进建议
    suggestions = report.get('improvement_suggestions', ['暂无'])
    suggestions_text = chr(10).join(['• ' + s for s in (suggestions if suggestions else ['暂无'])])

    # ── Phase 6: 五因子归因 (新增) ──
    attribution_data = report.get('attribution_data', {})
    five_factor = attribution_data.get('five_factor', {})
    five_factor_text = ""
    if five_factor.get('five_factor_available'):
        ff_lines = [
            "\n🔬 五因子归因分解",
            f"• 噪声交易: ${five_factor['noise_trades_pnl']:+,.0f}  (规则外交易造成的损失)",
            f"• 过早止盈: ${five_factor['early_exit_pnl']:+,.0f}  (提前平仓少赚的钱)",
            f"• 延迟止损: ${five_factor['late_exit_pnl']:+,.0f}  (死扛造成的额外亏损)",
            f"• 过度交易: ${five_factor['overtrading_pnl']:+,.0f}  (超出预期频率的交易)",
        ]
        score = five_factor.get('behavioral_score', {})
        if score:
            ff_lines.append(f"🧠 行为评分: {score.get('grade', '?')} ({score.get('score', '?')}/100)")

        disp = five_factor.get('disposition_effect', {})
        if disp.get('severity'):
            sev_emoji = "🔴" if disp['severity'] == 'high' else "🟡" if disp['severity'] == 'medium' else "🟢"
            ff_lines.append(f"{sev_emoji} 处置效应: {disp['severity'].upper()} (亏损/盈利持仓 = {disp.get('loser_winner_hold_ratio', '?')}x)")

        chase = five_factor.get('chasing', {})
        if chase.get('severity') and chase['severity'] != 'insufficient_data':
            sev_emoji = "🔴" if chase['severity'] == 'high' else "🟡" if chase['severity'] == 'medium' else "🟢"
            ff_lines.append(f"{sev_emoji} 追涨倾向: {chase['severity'].upper()} (偏离 {chase.get('avg_deviation_pct', 0):.3f}%)")

        five_factor_text = "\n".join(ff_lines)

    # ── 逐笔交易详情 (新增) ──
    trade_detail_text = ""
    trade_analyses = report.get('trade_analyses', [])
    trades_summary = report.get('summary', {}).get('trades', [])

    if trade_analyses:
        td_lines = ["\n📋 逐笔交易明细"]
        for ta in trade_analyses:
            sym = ta.get('symbol', '')
            direction = ta.get('direction', '')
            dir_emoji = "📈" if direction == "LONG" else "📉" if direction == "SHORT" else ""
            entry = ta.get('entry_price', 0)
            exit_ = ta.get('exit_price', 0)
            pnl = ta.get('realized_pnl', ta.get('pnl', 0))
            pnl_pct = ta.get('pnl_pct', 0)
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            exit_reason = ta.get('exit_reason', '')
            holding = ta.get('holding_hours', 0)
            verdict = ta.get('verdict', '')

            td_lines.append(
                f"\n  {dir_emoji} {sym} ({direction})"
                f"\n    买入: ${entry:,.2f} → 卖出: ${exit_:,.2f}"
                f"\n    {pnl_emoji} 盈亏: ${pnl_sign}{pnl:,.2f} ({pnl_pct*100:+.1f}%) | 持仓: {holding:.1f}h"
                f"\n    退出: {exit_reason} | 判定: {verdict}"
            )
        trade_detail_text = "\n".join(td_lines)

    elif trades_summary:
        td_lines = ["\n📋 逐笔交易明细"]
        for t in trades_summary:
            sym = t.get('symbol', '')
            direction = t.get('direction', '')
            dir_emoji = "📈" if direction == "LONG" else "📉" if direction == "SHORT" else ""
            entry = t.get('entry_price', 0)
            exit_ = t.get('exit_price', 0)
            pnl = t.get('realized_pnl', t.get('pnl', 0))
            pnl_pct = t.get('pnl_pct', 0)
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            exit_reason = t.get('exit_reason', '')
            holding = t.get('holding_hours', 0)

            td_lines.append(
                f"\n  {dir_emoji} {sym} ({direction})"
                f"\n    买入: ${entry:,.2f} → 卖出: ${exit_:,.2f}"
                f"\n    {pnl_emoji} 盈亏: ${pnl_sign}{pnl:,.2f} ({pnl_pct*100:+.1f}%) | 持仓: {holding:.1f}h"
                f"\n    退出: {exit_reason}"
            )
        trade_detail_text = "\n".join(td_lines)

    return f"""📊 FTSeek-US 每日复盘 v{version} ({date_str})

{pnl_emoji} 交易统计
• 总交易：{total} 笔
• 已平仓：{closed} 笔
• 盈利笔数：{wins} 笔
• 胜率：{win_rate*100:.1f}%
• 总盈亏：${pnl_sign}{pnl:.2f}{score_text}{regime_text}

📈 信号漏斗
{funnel_text}
{rej_text}{cap_text}{plan_text}{verdict_text}{five_factor_text}{trade_detail_text}

📝 改进建议
{suggestions_text}

⏰ 生成时间：{datetime.now(ET).strftime('%Y-%m-%d %H:%M')} ET
"""


def _format_weekly_review(report: dict) -> str:
    """格式化周报为文本 (2026-04-25 升级: 与日报对齐 + 周度特有分析)"""
    period_start = report.get('period_start', '')
    period_end = report.get('period_end', '')
    summary = report.get('summary', {})
    
    total = summary.get('total_trades') or 0
    closed = summary.get('closed_trades') or 0
    wins = summary.get('winning_trades') or 0
    pnl = summary.get('total_pnl') or 0
    win_rate = summary.get('win_rate') or 0
    
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    
    # 资金曲线
    capital = report.get('capital_curve', {})
    cap_text = ""
    if capital and 'start_equity' in capital:
        cap_text = (
            f"\n💰 资金曲线\n"
            f"• 期初: ${capital['start_equity']:,.0f} → 期末: ${capital['end_equity']:,.0f}\n"
            f"• 周收益: ${pnl_sign}{capital.get('weekly_pnl', 0):,.2f} ({capital.get('weekly_return_pct', 0):.2f}%)\n"
            f"• 最大回撤: {capital.get('max_drawdown_pct', 0):.2f}%"
        )
    
    # 信号漏斗
    funnel = report.get('signal_funnel', {})
    funnel_text = f"• 周决策数: {funnel.get('total_decisions', 0)} 条"
    
    # 拒绝趋势
    rejections = report.get('rejection_trend') or {}
    rej_text = ""
    if (rejections.get('total') or 0) > 0:
        trend = rejections.get('trend', 'stable')
        trend_emoji = "📈" if trend == "increasing" else "📉" if trend == "decreasing" else "➡️"
        top = rejections.get('top_rejector', 'unknown')
        rej_text = (
            f"\n🚫 拒绝趋势 (总 {rejections['total']} 次)\n"
            f"• 趋势: {trend_emoji} {trend} | TOP 原因: {top}"
        )
    
    # 逐日对比
    daily_comp = report.get('daily_comparison', {})
    daily_text = ""
    if daily_comp.get('daily_stats'):
        daily_text = "\n📅 逐日统计"
        for day, stats in sorted(daily_comp['daily_stats'].items()):
            day_pnl = stats.get('pnl') or 0
            day_sign = "+" if day_pnl >= 0 else ""
            daily_text += f"\n  {day}: {stats['trades']} 笔, 盈亏 {day_sign}${day_pnl:,.2f}"
        best = daily_comp.get('best_day')
        if best:
            daily_text += f"\n  👑 最佳: {best}"
    
    # 一致性
    consistency = report.get('consistency_score', {})
    cons_text = ""
    if consistency.get('score') is not None and 'note' not in consistency:
        cons_emoji = "🟢" if consistency.get('interpretation') == 'high' else "🟡" if consistency.get('interpretation') == 'medium' else "🔴"
        cons_text = f"\n🎯 一致性: {cons_emoji} {consistency['score']:.2f} ({consistency.get('interpretation', 'unknown')})"
    
    # 策略表现
    strategy_text = ""
    strategies = report.get('strategy_performance', {}).get('strategy_breakdown', {})
    if strategies:
        strategy_text = "\n📈 策略表现"
        for k, v in strategies.items():
            strategy_text += f"\n  {k}: {v.get('trades', 0)} 笔, 胜率 {v.get('win_rate', 0)*100:.1f}%, 盈亏 ${v.get('pnl', 0):,.2f}"
    
    # 改进计划
    plan_items = report.get('improvement_plan', [])
    plan_text = chr(10).join([f"• {p.get('action', '')} [{p.get('priority', 'low')}]" for p in plan_items]) or '继续执行当前策略'
    
    return f"""📊 FTSeek-US 每周复盘

📅 周期：{period_start} 至 {period_end}

{pnl_emoji} 周度统计
• 总交易：{total} 笔 (已平仓 {closed})
• 盈利笔数：{wins} 笔
• 胜率：{win_rate*100:.1f}%
• 总盈亏：${pnl_sign}{pnl:,.2f}
• 平均每笔: ${summary.get('avg_pnl_per_trade', 0):,.2f}
{cap_text}

📈 信号漏斗
{funnel_text}
{rej_text}
{cons_text}
{daily_text}
{strategy_text}

📋 下周计划
{plan_text}

⏰ 生成时间：{datetime.now(ET).strftime('%Y-%m-%d %H:%M')} ET
"""


def _format_monthly_review(report: dict) -> str:
    """格式化月报为文本"""
    year = report.get('year', '')
    month = report.get('month', '')
    perf = report.get('performance', {})
    risk = report.get('risk_metrics', {})
    
    total = perf.get('total_trades', 0)
    win_rate = perf.get('win_rate', 0)
    pnl = perf.get('total_pnl', 0)
    sharpe = perf.get('sharpe_ratio', 0)
    profit_factor = perf.get('profit_factor', 0)
    
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    
    return f"""📊 FTSeek-US 每月复盘

📅 周期：{year}年{month}月

{pnl_emoji} 月度收益
• 总交易：{total} 笔
• 胜率：{win_rate*100:.1f}%
• 总盈亏：${pnl_sign}{pnl:.2f}
• 夏普比率：{sharpe:.2f}
• 盈利因子：{profit_factor:.2f}

⚠️ 风险指标
• 最大单笔亏损：${risk.get('max_single_loss', 0):.2f}
• 最大回撤：${risk.get('max_drawdown', 0):.2f}
• 止损执行率：{risk.get('stop_loss_execution_rate', 0)*100:.1f}%

📈 投票分析
• 平均信号强度：{report.get('voting_analysis', {}).get('avg_signal_strength', 0)}
• 平均置信度：{report.get('voting_analysis', {}).get('avg_signal_confidence', 0)*100:.1f}%

📋 下月计划
{chr(10).join(['• ' + p for p in report.get('next_month_plan', [])])}

⏰ 生成时间：{datetime.now(ET).strftime('%Y-%m-%d %H:%M')} ET
"""
