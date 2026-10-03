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
import time
import threading
from datetime import datetime, date, timedelta
from app.utils.timezone import now_et
from typing import TYPE_CHECKING, Callable
from enum import Enum
from app.core.paths import get_decisions_db, get_project_root

import pytz

if TYPE_CHECKING:
    from .scheduler_core import TradingScheduler

logger = logging.getLogger(__name__)

# 美东时区
ET = pytz.timezone("America/New_York")

# Task #30 系统性问题周度归纳 kill switch (2026-09-03, DEB-20260903-trade-attribution-
# zombie-watch 方案A, 用户裁"按建议执行"): analyze_weekly 唯一数据源 trade_attribution 表
# 止于 2026-07-14 (ADR-015 策略隔离), 周五触发只会对着空表静默无操作. 恢复条件: playbook 链
# 重接归因写入后置 True (与 review_loop 退役⑦ 恢复条件一致).
_TASK30_SYSTEMIC_ANALYZER_ENABLED = False

# Task #16 周频参数优化 kill switch (2026-10-03, DEB-20261003 P0-1, 用户裁「同意 停推送」):
# WeeklyOptimizer 头条建议 (sl=-0.4) 是列名 bug 伪影 — load_trade_rows 请求的
# max_drawdown_during_trade 列在生产 trade_records 不存在, pick() 静默回退 NULL ⇒
# SL 反事实从未触发, stop_loss_pct 不可识别 (5 网格值 1 个 pnl 序列), -0.4 = 并列 tie-break
# 恒选网格序首个, 连续 7 周输出常量; 对比基线 LIVE_CURRENT_PARAMS 虚构 (sl/tp/trail 三值
# 生产配置零命中); 4 参数中 trailing_stop_trigger 全仓零消费点、min_signal_strength
# playbook 链不读; OOS 窗口重叠泄漏 + 实评 n=9 + bootstrap CI 跨零 42 单位宽。
# LiveBacktestComparison 共享同一虚构基线 (import LIVE_CURRENT_PARAMS) ⇒ 同批停。
# 契约测试钉住: tests/test_weekly_optimizer_contracts.py (xfail-strict 绊线, 修复即 XPASS 报警)。
# 恢复条件: P2-6 (docs/debates/DEB-20261003-weekly-optimizer-p26-rebuild.md 草案) 用
# 轮询级模拟器 + playbook_horizon.yaml 真源重建并通过契约测试后置 True。
# 周报展示面已由 weekly_scorecard_carrier 第三块 (exit_shadow_report: 真实参数 vs 影子参数) 承接。
_TASK16_WEEKLY_OPTIMIZER_ENABLED = False



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
    except Exception as _silent_e:
        logger.debug(f"[silent_except:scheduler_triggers.py] {_silent_e}")

    hour = now_et.hour
    minute = now_et.minute
    _mins = hour * 60 + minute

    # ── R-8b (2026-08-23 DEB-20260823): 半日市动态收盘 ──
    # 原实现硬编码 `hour < 16`, 不处理提前收盘日。holiday_calendar 已有
    # US_EARLY_CLOSE_DATES (含 2026-11-27 感恩节次日 13:00 收) + get_us_close_minutes_et,
    # 但本函数 —— 主调度的市场时段 SSOT (消费方: signal_pipeline:161 / llm_pipeline:172 /
    # scheduler_core._is_market_open / position_monitor / market_data_stage / signal_stage)
    # —— 从未使用它。后果: 半日市当天 ET 13:00-16:00 被判 TRADING, 3 小时向已闭市市场
    # 生成信号并提交单 (playbook 入场触发窗截止 14:00, 故 13:00-14:00 真会下单)。
    # 取值失败兜底 960 (16:00), 与 get_us_close_minutes_et docstring 约定一致。
    _close_mins = 16 * 60
    try:
        from app.utils.holiday_calendar import get_us_close_minutes_et
        _close_mins = int(get_us_close_minutes_et(now_et.date()))
    except Exception as _ec_err:
        logger.debug(f"[market_phase] 收盘时刻取值失败, 兜底 16:00: {_ec_err}")

    # 盘中: 09:30 - 当日收盘 (常规 16:00 / 半日市 13:00)
    if 570 <= _mins < _close_mins:
        return MarketPhase.TRADING

    # 盘前: 04:00 - 09:30
    if 240 <= _mins < 570:
        return MarketPhase.PRE_MARKET

    # 盘后夜盘: 当日收盘 - 20:00 (半日市从 13:00 起即为盘后)
    if _close_mins <= _mins < 20 * 60:
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


def sleep_if_market_closed(
    interval_seconds: int,
    running_check: "Callable[[], bool] | None" = None,
) -> bool:
    """
    统一假日/休市守卫 — 所有后台循环的单一入口 (2026-06-19)

    根因: 之前每个循环单独检查 get_market_phase()，新增循环容易遗漏，
    导致假日/周末空跑 (Juneteenth 当天 ORB 1221 ticks, News 1864 次, VIX 761 次)。

    修复: 提供统一守卫函数，任何循环只需一行:
        if sleep_if_market_closed(60, lambda: self.running): continue

    这是唯一被允许的循环休眠方式。直接用 time.sleep() 或 for+sleep 是反模式。

    Args:
        interval_seconds: 休眠时长 (秒)
        running_check: 检查循环是否仍在运行的 callable (默认始终 True)

    Returns:
        True = 市场关闭，已休眠，调用方应 continue
        False = 市场开放，调用方应继续执行
    """
    phase = get_market_phase()
    if phase != MarketPhase.WEEKEND:
        return False

    # 市场关闭 — 休眠 interval_seconds (可中断)
    _check = running_check or (lambda: True)
    for _ in range(int(interval_seconds)):
        if not _check():
            break
        time.sleep(1)
    return True


def interruptible_sleep(
    seconds: int,
    running_check: "Callable[[], bool] | None" = None,
) -> bool:
    """
    统一的可中断休眠 — 所有后台循环的 sleep 原语 (2026-06-19)

    这是唯一被允许的循环休眠方式。替代:
      ❌ for _ in range(N): time.sleep(1)
      ❌ time.sleep(N)
      ✅ interruptible_sleep(N, lambda: self.running)

    自动包含假日/周末守卫: 市场关闭时不消耗 CPU，直接跳过。

    Args:
        seconds: 休眠时长 (秒)
        running_check: 检查循环是否仍在运行的 callable (默认始终 True)

    Returns:
        True = 被中断 (running_check 返回 False)，调用方应 return/break
        False = 正常休眠完成，调用方应继续下一轮
    """
    _check = running_check or (lambda: True)

    # 假日/周末: 直接跳过，不消耗任何资源
    if get_market_phase() == MarketPhase.WEEKEND:
        return False

    # 正常休眠: 可中断
    for _ in range(int(seconds)):
        if not _check():
            return True  # 被中断
        time.sleep(1)
    return False


class MarketClosedError(Exception):
    """当市场关闭时抛出，用于提前退出循环体"""
    pass


def assert_market_open():
    """
    断言市场当前开放。如果关闭则抛出 MarketClosedError。

    用于循环体开头的守卫:
        while self.running:
            try:
                assert_market_open()
                # ... 交易逻辑 ...
            except MarketClosedError:
                interruptible_sleep(300, lambda: self.running)
    """
    if get_market_phase() == MarketPhase.WEEKEND:
        raise MarketClosedError("Market is closed (weekend/holiday)")


def is_in_cooldown(scheduler: 'TradingScheduler', symbol: str) -> bool:
    """
    检查信号冷却

    P1 修复 (2026-05-04): 使用 scheduler._open_position_lock 保护，
    防止 signal_pipeline 和 force_scan 线程并发读写 _signal_cooldown 导致竞态。
    H4 (2026-06-03): 顺带清理过期条目, 防止内存泄漏
    """
    with scheduler._open_position_lock:
        # H4: 清理过期条目 (每 10 次调用清理一次, 降低开销)
        _cd = scheduler._signal_cooldown
        if len(_cd) > 50 and hash(symbol) % 10 == 0:
            _cutoff = now_et() - __import__('datetime').timedelta(minutes=scheduler.SIGNAL_COOLDOWN_MINUTES * 2)
            _expired = [s for s, t in _cd.items() if t < _cutoff]
            for s in _expired:
                del _cd[s]

        if symbol not in _cd:
            return False
        elapsed = (now_et() - _cd[symbol]).total_seconds() / 60
        return elapsed < scheduler.SIGNAL_COOLDOWN_MINUTES


def set_cooldown(scheduler: 'TradingScheduler', symbol: str):
    """
    设置信号冷却

    P1 修复 (2026-05-04): 使用 scheduler._open_position_lock 保护写入。
    """
    with scheduler._open_position_lock:
        scheduler._signal_cooldown[symbol] = now_et()


def _check_recommendation_accuracy(scheduler):
    """
    16:30+ ET 生成并推送推荐准确率报告 (Task #40, 2026-05-28)

    判定条件:
    - hour >= 16 且 minute >= 30  (避免与 16:00 复盘抢跡)
    - 当日未执行过 (日期级幂等性)
    - recommendations.accuracy_tracking.enabled = true

    该函数不报错, 任何异常需被调用方 try-except 包裹。
    """
    try:
        now_et = datetime.now(ET)
        # 16:30+ ET 门控
        if not (now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 30)):
            return
        # 幂等性: 当日仅运行一次
        if getattr(scheduler, "_recommendation_accuracy_date", None) == now_et.date():
            return

        from app.core.config_container import get_config
        cfg = get_config()
        if not cfg.get_dotted("recommendations.accuracy_tracking.enabled", False):
            return

        from app.recommendations.accuracy_tracker import AccuracyTracker
        from app.recommendations.push_trigger import PremarketPushTrigger

        tracker = AccuracyTracker()
        report = tracker.verify_yesterday()

        # 注入滚动准确率到报告
        rolling = tracker.get_rolling_accuracy()
        if rolling:
            report["rolling_accuracy"] = rolling

        if report and report.get("total", 0) > 0:
            trigger = PremarketPushTrigger()
            trigger.send_accuracy_report(report)
            logger.info(
                f"[Recommendations] 准确率报告已推送: "
                f"{report.get('accuracy_pct', 0):.1f}% (n={report.get('total', 0)})"
            )
        else:
            logger.info("[Recommendations] 准确率报告: 当日无可评估推荐")

        # 设置幂等标记 (无论是否有数据, 当日不重复运行)
        scheduler._recommendation_accuracy_date = now_et.date()
    except ImportError as ie:
        logger.debug(f"[Recommendations] 准确率模块未就绪 (跳过): {ie}")
    except Exception as e:
        logger.warning(f"[Recommendations] 准确率评估异常: {e}")


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
        
        # 作战计划 — 2026-08-01 断线: battle plan 07-11 冻结, 陈旧 rationale 喂 LLM 系误导。
        battle_plan = {}
        try:
            search_terms = []
            for t in trades:
                search_terms.append(t.get("symbol", ""))
                search_terms.append(t.get("exit_reason", ""))
            query = " ".join(search_terms[:10])
            if query.strip():
                bridge.search_lessons(query, top_k=3)
        except Exception as _silent_e:
            logger.debug(f"[silent_except:scheduler_triggers.py] {_silent_e}")

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
        # Task #40 (2026-05-28): 16:30+ ET 推荐准确率报告触发
        # 独立于日复盘的幂等门（拥有自己的 _recommendation_accuracy_date 标记）
        # 这样无论日复盘是否已运行, 16:30 准确率报告都能独立触发
        try:
            _check_recommendation_accuracy(scheduler)
        except Exception as _rae:
            logger.debug(f"[Recommendations] 准确率触发异常（不影响主流程）: {_rae}")

        # 2026-07-28 修复: 内存幂等标记重启即丢 — 今晨实锤 04:30 正常跑后 06:37 重启又跑
        #   一轮 (3 LLM 辩论 + 日K配额消耗双倍)。改文件持久化 (llm_scan_state 同款先例),
        #   内存标记保留作同进程快速路径。
        from pathlib import Path as _RP
        _review_marker = _RP("data/.last_daily_review")
        _done = getattr(scheduler, '_last_review_date', None) == now_et.date()
        if not _done:
            try:
                _done = _review_marker.read_text().strip() == now_et.date().isoformat()
            except Exception:
                _done = False
        if not _done:
            scheduler._last_review_date = now_et.date()
            try:
                _review_marker.write_text(now_et.date().isoformat())
            except Exception:
                pass  # 标记写失败不阻断复盘 (退化回内存幂等)
            try:
                from app.review.daily_review import generate_daily_review
                review = generate_daily_review(now_et.date().isoformat())
                _push_review_text(scheduler, _format_daily_review(report=review), "日报")
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
                        # Task #46 (2026-05-28): 从 SSOT 路径读取, 避免硬编码
                        from app.core.config_container import get_config
                        from app.core.config_keys import ConfigKeys
                        cfg = get_config()
                        if cfg is not None:
                            loop_context["MIN_CONFIDENCE"] = float(
                                cfg.get_dotted(ConfigKeys.MIN_CONFIDENCE.value, 0.70)
                            )
                            loop_context["SIGNAL_MIN_SCORE"] = int(
                                cfg.get_dotted(ConfigKeys.MIN_EFFECTIVE_STRENGTH.value, 50)
                            )
                            loop_context["MAX_POSITION_PER_SYMBOL"] = float(
                                # [DEB-fullchain #19] 回退值与 yaml SSOT 0.08 同步 (原 0.25)
                                cfg.get_dotted("risk.position.max_per_symbol", 0.08)
                            )
                        else:
                            loop_context["MIN_CONFIDENCE"] = 0.70
                            loop_context["SIGNAL_MIN_SCORE"] = 50
                            loop_context["MAX_POSITION_PER_SYMBOL"] = 0.08
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

                # ── Task #12 (2026-05-27): 信号质量追踪与反馈闭环 ──
                # 生成 confidence -> 胜率映射、策略表现、因子贡献度、阈值调整建议
                # 仅输出建议, 不自动修改配置
                try:
                    from app.analysis.signal_quality_tracker import run_signal_quality_daily
                    sq_summary = run_signal_quality_daily(as_of=now_et.date())
                    if sq_summary:
                        recs = sq_summary.get("threshold_recommendations", {})
                        actions = recs.get("strategy_actions", []) or []
                        logger.info(
                            "[SignalQuality] 每日反馈完成 | trades=%d | 推荐 min_conf=%s | 高危策略=%d",
                            sq_summary.get("total_closed_trades", 0),
                            recs.get("suggested_min_signal_confidence"),
                            len(actions),
                        )
                        # 高危策略告警推送 (近 10 笔胜率 < 20%)
                        if scheduler.notifier and actions:
                            lines = ["⚠️ [信号质量] 高危策略告警"]
                            for a in actions:
                                lines.append(
                                    f"  - {a['category']}: 近{a['recent_trades']}笔胜率 "
                                    f"{a['recent_win_rate']:.1%} → {a['recommendation']}"
                                )
                            if recs.get("anomalies"):
                                lines.append("异常: " + "; ".join(recs["anomalies"]))
                            try:
                                scheduler.notifier.send_text("\n".join(lines))
                            except Exception as _silent_e:
                                logger.debug(f"[silent_except:scheduler_triggers.py] {_silent_e}")
                except Exception as sqe:
                    logger.debug(f"[SignalQuality] 每日反馈失败 (不影响主流程): {sqe}")
                # ── 信号质量追踪结束 ──

                # ── Playbook 指令命中率追踪 ──
                # 对标当日 Playbook 指令 vs 实际交易, 输出 hit_rate / traded_rate / unplanned_count
                try:
                    from app.analysis.plan_effectiveness import PlanEffectivenessTracker
                    tracker = PlanEffectivenessTracker()
                    plan_report = tracker.evaluate_daily_plan(now_et.date().isoformat())
                    if plan_report and plan_report.get("plan_available") and scheduler.notifier:
                        hit_rate = plan_report.get("hit_rate", 0) or 0
                        traded_rate = plan_report.get("traded_rate", 0) or 0
                        msg = (
                            f"📊 Playbook 指令效果: 命中率 {hit_rate:.0%}, 执行率 {traded_rate:.0%}"
                        )
                        try:
                            scheduler.notifier.send_text(msg)
                        except Exception as _silent_e:
                            logger.debug(f"[silent_except:scheduler_triggers.py] {_silent_e}")
                except Exception as pe:
                    logger.debug(f"[计划效果] 评估失败: {pe}")
                # ── 盘前计划命中率追踪结束 ──

                # ── Factor Pipeline 收盘反馈闭环 — 已下线 (2026-08-28 过期链路清理) ──
                # 原每日调用 app.plan/plan_feedback.py:run_feedback, 三条输入全部已死:
                #   ① data/factor_screen_latest.json 文件不存在 (f4abf2c0 已定性为死文件,
                #      _load_factor_screen 缺失→降级, factor_ic 恒 {});
                #   ② data/daily_battle_plan_us.json 冻结在 date=2026-07-31 (ADR-012 旧作战
                #      计划轨道下线) → _load_plan 日期不匹配恒 None, plan_effectiveness 恒 {'count': 0};
                #   ③ match_plan_trades 一旦可达必崩: SQL 引用不存在的列 close_time/strategy,
                #      且 DB_PATH 指向废弃同名单库 ftseek_us_live.db, trade_mode!='simulate'
                #      又排除 718/726 行 (当前轨道全部 simulate)。
                # 产物实锤: feedback_2026-08-06…27 恒 186 字节全空 → 推送分支 (count>0) 从未触发,
                # 失败仅 logger.debug (LOG_LEVEL=INFO 不可见) — 典型"降级不报警"。
                # 指令命中率反馈由上方 PlanEffectivenessTracker (playbook_instructions 轨道) 承担。
                # 恢复: git revert 本块 + 先修 plan_feedback 三条输入。

                # Phase 5: 通过 Agent 桥接触发盘后全链路归因
                if hasattr(scheduler, 'agent_bridge') and scheduler.agent_bridge:
                    try:
                        _trigger_eod_agent_review(scheduler, review)
                    except Exception as ae:
                        logger.debug(f"[Agent] 触发盘后归因失败 (不影响主流程): {ae}")
                
                # Phase 6: REMOVED (2026-06-19) — ShadowTracker 变体测试不再使用
                # 改用 Playbook shadow 模式做策略验证
                
                # Phase 7: REMOVED (2026-06-19) — Shadow EOD 批量关闭不再需要
                # shadow_trades 表已无数据，Playbook shadow 走独立生命周期


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
                _push_review_text(scheduler, _format_weekly_review(report=report), "周报")
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

                # ── Task #16 (2026-05-27): 周频参数优化 ──
                # 周末/周五盘后运行回测驱动的参数优化, 仅产出建议报告, 不自动落地
                # 2026-10-03 DEB-20261003 P0-1 停推送 (用户裁「同意 停推送」): 输出无裁决价值
                # (头条 sl=-0.4 是列名 bug + tie-break 伪影, live 基线虚构), 修好前每周推送
                # 带 ✅ 的假建议是净负。kill switch 见 _TASK16_WEEKLY_OPTIMIZER_ENABLED 注释;
                # 恢复条件 = P2-6 轮询级模拟器重建 (DEB-20261003 草案) 落地并通过契约测试。
                # 真实参数 vs 影子参数对比已由周六记分牌载体承接 (exit_shadow_report 块)。
                try:
                    if not _TASK16_WEEKLY_OPTIMIZER_ENABLED:
                        raise RuntimeError(
                            "Task#16 WeeklyOptimizer/LiveBacktestCompare 已停推送 "
                            "(DEB-20261003 P0-1: 输出建立在列名 bug + 虚构 live 基线上, "
                            "恢复条件见 _TASK16_WEEKLY_OPTIMIZER_ENABLED 注释)"
                        )
                    from app.backtest.weekly_optimizer import run_weekly_optimization
                    from app.backtest.live_backtest_comparison import run_comparison
                    opt_report = run_weekly_optimization(notifier=scheduler.notifier)
                    logger.info(
                        f"[WeeklyOptimizer] 完成: best_score={opt_report.best_score} "
                        f"overfit={opt_report.overfit_warning}"
                    )
                    cmp_report = run_comparison(notifier=scheduler.notifier)
                    logger.info(
                        f"[LiveBacktestCompare] 完成: live={cmp_report.n_live} "
                        f"sharpe_diff={cmp_report.diffs.get('sharpe_diff')}"
                    )
                except Exception as wo_err:
                    logger.debug(f"[WeeklyOptimizer] 失败 (不影响主流程): {wo_err}")

                # ── Task #30 (2026-05-27): 系统性问题周度归纳 ──
                # 从 trade_attribution 聚合本周系统性问题, 按 systemic_pattern 聚类
                # 然后推送 Top-3 高优先修复项。于周五 16:00+ ET 触发
                # (避免依赖周末 scheduler 运行)。
                # 2026-09-03 DEB-20260903 方案A 退役 (用户裁"按建议执行"): 数据源死表,
                # 恢复条件见 _TASK30_SYSTEMIC_ANALYZER_ENABLED 注释。
                try:
                    if not _TASK30_SYSTEMIC_ANALYZER_ENABLED:
                        raise RuntimeError(
                            "Task#30 SystemicIssueAnalyzer 已退役 "
                            "(DEB-20260903-trade-attribution-zombie-watch: "
                            "trade_attribution 表止于 2026-07-14)"
                        )
                    from app.analysis.systemic_issue_analyzer import SystemicIssueAnalyzer
                    analyzer = SystemicIssueAnalyzer()
                    weekly_report = analyzer.analyze_weekly()
                    if weekly_report and weekly_report.get("total_systemic_issues", 0) > 0:
                        patterns = weekly_report.get("patterns", {})
                        top_issues = sorted(
                            patterns.items(),
                            key=lambda x: x[1].get("total_loss", 0),
                        )[:3]
                        msg_lines = [
                            f"📋 周度系统性问题报告 ({weekly_report['week']})",
                            f"共 {weekly_report['total_systemic_issues']} 个系统性问题",
                        ]
                        for pattern, data in top_issues:
                            msg_lines.append(
                                f"  • {pattern}: {data['count']}次, "
                                f"损失 ${abs(data.get('total_loss', 0)):.0f}, "
                                f"严重度={data.get('severity', 'low')}"
                            )
                        if scheduler.notifier:
                            try:
                                scheduler.notifier.send_text("\n".join(msg_lines))
                            except Exception as _silent_e:
                                logger.debug(f"[silent_except:scheduler_triggers.py] {_silent_e}")
                        logger.info(
                            f"[系统性问题] 周报生成: {weekly_report['week']}, "
                            f"issues={weekly_report['total_systemic_issues']}, "
                            f"patterns={len(patterns)}"
                        )
                    else:
                        logger.info(f"[系统性问题] 本周无系统性问题 ({weekly_report.get('week') if weekly_report else ''})")
                except Exception as sie:
                    logger.debug(f"[系统性问题] 周度分析失败: {sie}")
                # ── Task #30 结束 ──
            except Exception as e:
                logger.error(f"[周报] 复盘推送失败：{e}", exc_info=True)
            
            # ── LLM 周度假设生成 (角色重定位 2026-05-13) ──
            # 每周五盘后调 LLM 生成 1-3 个参数调整提议
            # 2026-07-31 议题⑧ (用户裁"不看"): 默认关停 — 提议推送无人消费, 冻结期纯 LLM 成本
            if scheduler._last_weekly_review == week_end:  # 只在周报复盘成功后触发
                try:
                    from app.core.config_container import get_config as _wp_gc
                    if not bool(_wp_gc().get_dotted("llm_pushes.weekly_proposer_enabled", False)):
                        raise RuntimeError("weekly_proposer 已关停 (llm_pushes.weekly_proposer_enabled=false)")
                    from app.llm.weekly_proposer import get_weekly_proposer
                    from app.evolution.improvement_pipeline import get_pipeline
                    
                    proposer = get_weekly_proposer()
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
    2026-08-01: 哨兵文件幂等 (内存标记重启即丢, 1日窗口内重启会重复推送)
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
        
        sentinel = os.path.join(os.path.dirname(__file__), '..', '..', 'data', '.last_monthly_review')
        try:
            if os.path.exists(sentinel) and open(sentinel).read().strip() == review_key:
                return
        except Exception:
            pass
        if getattr(scheduler, '_last_monthly_review', None) == review_key:
            return
        scheduler._last_monthly_review = review_key
        try:
            from app.review.monthly_review import generate_monthly_review
            report = generate_monthly_review(year, month)
            _push_review_text(scheduler, _format_monthly_review(report=report), "月报")
            logger.info(f"[月报] 复盘推送完成：{year}年{month}月")
            try:
                with open(sentinel, 'w') as _f:
                    _f.write(review_key)
            except Exception:
                pass
            
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
    # 根因修复 (2026-06-10): AFTER_HOURS 也必须跳过 — 盘后无实时行情，
    # get_full_market_context 级联产生 ~8500 条/天的噪音 WARNING
    if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT, MarketPhase.PRE_MARKET, MarketPhase.AFTER_HOURS):
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


def _push_review_text(scheduler, text: str, label: str) -> None:
    """复盘报告推送 (2026-08-01, 用户裁): 走 PushHub — 钉钉失败自动 fallback 飞书 + 全失败落盘。
    旧路径 scheduler.notifier.send_text 单通道无重试, webhook 故障 = 报告静默丢失。
    PushHub 不可用时退化旧路径。"""
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
        first_nl = text.find("\n")
        title = text[:first_nl] if first_nl > 0 else text[:60]
        body = text[first_nl + 1:] if first_nl > 0 else ""
        ok = get_push_hub().push(PushMessage(
            title=title, body=body, level=Level.NORMAL, category=Category.SYSTEM,
            dedup_key=f"review:{label}:{title}",
        ))
        if not ok:
            logger.warning(f"[{label}] PushHub 全通道失败 (已落盘 push_failures.jsonl)")
    except Exception as e:
        logger.warning(f"[{label}] PushHub 异常 ({e}), 退化单通道")
        if scheduler.notifier:
            scheduler.notifier.send_text(text)


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
                        # data_available=False 是"无数据"而非"失败" (如执行质量依赖未填充的 trade_attribution)
                        status = '✅' if status else 'N/A'
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
    
    # 信号漏斗 (2026-08-01: 数据源已改 playbook_instructions, 标签随源)
    funnel = report.get('signal_funnel', {})
    _dec_label = "指令数" if funnel.get('source') == 'playbook_instructions' else "决策数"
    funnel_text = f"• {_dec_label}: {funnel.get('total_decisions', 0)} 条"
    _bd = funnel.get('by_direction_strength') or {}
    if _bd:
        funnel_text += " (" + ", ".join(f"{k}:{v}" for k, v in sorted(_bd.items())) + ")"
    _source_breakdown = funnel.get('by_source_direction') or {}
    if _source_breakdown:
        funnel_text += "\n• 来源: " + ", ".join(
            f"{source}:{count}" for source, count in _source_breakdown.items()
        )
    
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
    
    # v2.0: 计划对比 (2026-08-01: 基准=当日 playbook 指令)
    plan_text = ""
    pva = report.get('plan_vs_actual', {})
    if pva.get('plan_available'):
        executed = len(pva.get('executed_targets', []))
        missed = len(pva.get('missed_opportunities', []))
        unplanned = len(pva.get('unplanned_trades', []))
        rate = pva.get('execution_rate')
        plan_text = (
            f"\n📋 计划执行 (playbook 指令)\n"
            f"• 已执行: {executed} | 未成交: {missed} | 计划外: {unplanned}"
            + (f" | 执行率: {rate*100:.0f}%" if rate is not None else "")
            + (" ⚠️计划外>0=链路异常" if unplanned else "")
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

    # ── Phase 6: 五因子归因 (2026-08-01 退役标注, 用户裁"做减法") ──
    # 该块三重失真: ①过早止盈/延迟止损系死分支 (shadow_account.py else 内条件与
    # in_rule_range 互斥, 恒 $0) ②噪声符号与标签相反 (负值实为规则外盈利)
    # ③规则基线 (旧持仓天数规则) 已退役, 日内 playbook 单全被判"规则外噪声"。
    # 停止推送数字, JSON 保留备查; 模块整体待 08-07 后下线。
    attribution_data = report.get('attribution_data', {})
    five_factor = attribution_data.get('five_factor', {})
    five_factor_text = ""
    if five_factor.get('five_factor_available'):
        five_factor_text = ("\n🔬 五因子归因: ⚠️ 基线已退役 (旧持仓规则 vs 日内 playbook), "
                            "读数无效 — 模块待 08-07 后下线")

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
            # 2026-08-01 修复: 循环变量原名 pnl/pnl_sign/pnl_emoji 会覆盖头部总盈亏变量
            # (07-31 推送"总盈亏 $-0.48"=最后一笔 ORCL, 真值 -140.06) — 改名隔离
            t_pnl = ta.get('realized_pnl', ta.get('pnl', 0))
            pnl_pct = ta.get('pnl_pct', 0)
            t_pnl_sign = "+" if t_pnl >= 0 else ""
            t_pnl_emoji = "🟢" if t_pnl > 0 else "🔴" if t_pnl < 0 else "⚪"
            exit_reason = ta.get('exit_reason', '')
            holding = ta.get('holding_hours', 0)
            verdict = ta.get('verdict', '')

            td_lines.append(
                f"\n  {dir_emoji} {sym} ({direction})"
                f"\n    买入: ${entry:,.2f} → 卖出: ${exit_:,.2f}"
                f"\n    {t_pnl_emoji} 盈亏: ${t_pnl_sign}{t_pnl:,.2f} ({pnl_pct*100:+.1f}%) | 持仓: {holding:.1f}h"
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
            t_pnl = t.get('realized_pnl', t.get('pnl', 0))
            pnl_pct = t.get('pnl_pct', 0)
            t_pnl_sign = "+" if t_pnl >= 0 else ""
            t_pnl_emoji = "🟢" if t_pnl > 0 else "🔴" if t_pnl < 0 else "⚪"
            exit_reason = t.get('exit_reason', '')
            holding = t.get('holding_hours', 0)

            td_lines.append(
                f"\n  {dir_emoji} {sym} ({direction})"
                f"\n    买入: ${entry:,.2f} → 卖出: ${exit_:,.2f}"
                f"\n    {t_pnl_emoji} 盈亏: ${t_pnl_sign}{t_pnl:,.2f} ({pnl_pct*100:+.1f}%) | 持仓: {holding:.1f}h"
                f"\n    退出: {exit_reason}"
            )
        trade_detail_text = "\n".join(td_lines)

    # 净口径头部 (2026-08-01, DEB-20260730 D1): 毛/费/净三列 + 净胜率 + 假盈利
    # 旧 JSON 无净字段 → 回退毛口径渲染
    pnl_net = summary.get('total_pnl_net')
    if pnl_net is not None:
        _fees = summary.get('fees_total') or 0
        _wr_net = (summary.get('win_rate_net') or 0) * 100
        _fake = summary.get('fake_wins', 0)
        header_emoji = "🟢" if pnl_net > 0 else "🔴" if pnl_net < 0 else "⚪"
        pnl_lines = (
            f"• 胜率：{win_rate*100:.1f}% (净 {_wr_net:.1f}%"
            + (f", 假盈利 {_fake} 笔" if _fake else "") + ")\n"
            f"• 总盈亏：毛 ${pnl:+.2f} | 费 ${_fees:.2f} | 净 ${pnl_net:+.2f}"
        )
    else:
        header_emoji = pnl_emoji
        pnl_lines = f"• 胜率：{win_rate*100:.1f}%\n• 总盈亏：${pnl_sign}{pnl:.2f}"

    return f"""📊 FTSeek-US 每日复盘 v{version} ({date_str})

{header_emoji} 交易统计
• 总交易：{total} 笔
• 已平仓：{closed} 笔
• 盈利笔数：{wins} 笔
{pnl_lines}{score_text}{regime_text}

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
    
    # 信号漏斗 (2026-08-01: 数据源已改 playbook_instructions, 标签随源)
    funnel = report.get('signal_funnel', {})
    _wdec_label = "周指令数" if funnel.get('source') == 'playbook_instructions' else "周决策数"
    funnel_text = f"• {_wdec_label}: {funnel.get('total_decisions', 0)} 条"
    _wsource_breakdown = funnel.get('by_source_direction') or {}
    if _wsource_breakdown:
        funnel_text += "\n• 来源: " + ", ".join(
            f"{source}:{count}" for source, count in _wsource_breakdown.items()
        )
    
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

    # RFC-061 cohort A/B 退出实验 (OKR KR2.1/KR2.2 退出对齐 T+5 alpha 的增量)
    cohort_text = ""
    cohort = report.get('cohort_ab') or {}
    if cohort and not cohort.get('error'):
        try:
            from app.review.cohort_ab import format_cohort_text
            cohort_text = "\n" + format_cohort_text(cohort)
        except Exception:
            cohort_text = ""

    # 2026-08-01 (议题四尺子固化): 方向稳定性 + 方向×regime 交叉 (已执行口径, 双基准戒律:
    #   此处 T+1 命中为 entry 基准; open 基准孪生读数见周六 direction_scorecard 四象限块)
    dax_text = ""
    dax = report.get('direction_axes') or {}
    if dax and not dax.get('error'):
        stab = dax.get('stability_exec') or {}
        if stab:
            dax_text = "\n🧭 方向稳定性 (已执行·近5周)"
            for d in ("BUY", "SELL_SHORT"):
                s = stab.get(d)
                if not s:
                    continue
                icon = "🟢" if s["streak"] >= 4 else "🔴" if s["streak"] <= -4 else "🟡"
                dax_text += f"\n  {d}: {icon} {s['verdict']} (streak {s['streak']:+d})"
        cross = dax.get('cross_exec') or {}
        rows = [(d, rg, a) for d, rs in cross.items() for rg, a in rs.items()
                if a.get("n", 0) >= 8]
        if rows:
            dax_text += "\n  方向×环境: " + " | ".join(
                f"{d}/{rg} n{a['n']} {a['hit_t1']}%" for d, rg, a in sorted(rows))

    # 净口径头部 (2026-08-01, DEB-20260730 D1), 旧 JSON 无净字段回退毛口径
    _wpnl_net = summary.get('total_pnl_net')
    if _wpnl_net is not None:
        _wfees = summary.get('fees_total') or 0
        _wwr_net = (summary.get('win_rate_net') or 0) * 100
        _wfake = summary.get('fake_wins', 0)
        header_emoji = "🟢" if _wpnl_net > 0 else "🔴" if _wpnl_net < 0 else "⚪"
        pnl_lines = (
            f"• 胜率：{win_rate*100:.1f}% (净 {_wwr_net:.1f}%"
            + (f", 假盈利 {_wfake} 笔" if _wfake else "") + ")\n"
            f"• 总盈亏：毛 ${pnl:+,.2f} | 费 ${_wfees:,.2f} | 净 ${_wpnl_net:+,.2f}\n"
            f"• 平均每笔: 毛 ${summary.get('avg_pnl_per_trade', 0):+,.2f} | 净 ${(_wpnl_net / closed if closed else 0):+,.2f}"
        )
    else:
        header_emoji = pnl_emoji
        pnl_lines = (f"• 胜率：{win_rate*100:.1f}%\n• 总盈亏：${pnl_sign}{pnl:,.2f}\n"
                     f"• 平均每笔: ${summary.get('avg_pnl_per_trade', 0):,.2f}")

    return f"""📊 FTSeek-US 每周复盘

📅 周期：{period_start} 至 {period_end}

{header_emoji} 周度统计
• 总交易：{total} 笔 (已平仓 {closed})
• 盈利笔数：{wins} 笔
{pnl_lines}
{cap_text}

📈 信号漏斗
{funnel_text}
{rej_text}
{cons_text}
{daily_text}
{strategy_text}
{cohort_text}
{dax_text}

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


# ============================================================================
# Task #49 (2026-05-28): 每日 09:30 ET 持仓对账任务
# ============================================================================
def check_position_reconcile(scheduler: 'TradingScheduler'):
    """每日 09:30 ET 执行持仓对账 (内存 vs DB 一致性检测)

    触发条件:
      - 美东时间 09:30 ≤ hh:mm ≤ 09:45 (宽窗口避免扫描间隔跳过)
      - 当日未执行过 (日级幂等: scheduler._last_reconcile_date)

    调用 USPositionSync.daily_reconcile():
      - 内存/DB 一致 → logger.info
      - 不一致 → logger.critical + 飞书告警
      - 异常 → logger.error (不中断主流程)

    Args:
        scheduler: TradingScheduler 实例 (提供 _last_reconcile_date 幂等状态)
    """
    now_et = datetime.now(ET)
    # 窗口: 09:30 ~ 09:45 ET
    if not (now_et.hour == 9 and 30 <= now_et.minute <= 45):
        return
    # 日级幂等
    if getattr(scheduler, '_last_reconcile_date', None) == now_et.date():
        return
    scheduler._last_reconcile_date = now_et.date()

    try:
        from app.core.position_sync import get_us_position_sync
        sync = get_us_position_sync()
        if sync:
            futu_pos = sync.query_broker_positions()
            sync._verify_and_sync_capital(futu_pos or {})
            logger.info(f"[定时对账] 持仓一致性检查完成: {now_et.strftime('%Y-%m-%d %H:%M')} ET")
    except Exception as e:
        logger.error(f"[定时对账] 执行异常: {e}", exc_info=True)
        # 不中断主流程
