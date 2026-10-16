"""盘后追踪流 (阶段 6 迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_logger = logging.getLogger(__name__)

# 2026-10-15 (审计 P2-6): 盘后推送持久去重状态 — 内存标志 _playbook_tracked_date
# 跨重启即丢 + hub 去重仅 60s 内存 ⇒ 持夜仓日每次重启都重推 (10-14 实证推了 3 次)。
_TRACK_PUSH_STATE = (
    Path(__file__).resolve().parents[3] / "data"
    / "playbook_evening_tracking_push_state.json"
)  # 绝对路径锚定 (2026-10-16 巡检 P2-10): 相对路径依赖进程 cwd, 漂移即静默失去去重


def _evening_push_fingerprint(db, today: str) -> str:
    """当日指令终态指纹: 任一指令 outcome/退出价/净额变化 ⇒ 指纹变 ⇒ 允许重推。
    2026-10-16 修: pnl_net → pnl (playbook_instructions 无 pnl_net 列, 原分量恒 "None")。"""
    rows = sorted(
        (
            str(i.get("id")), str(i.get("outcome")),
            str(i.get("exit_price")), str(i.get("pnl")),
        )
        for i in (db.get_instructions_by_date(today) or [])
    )
    return hashlib.md5(json.dumps(rows).encode("utf-8")).hexdigest()


def _evening_push_already_sent(db, today: str, fp: str) -> bool:
    try:
        st = json.loads(_TRACK_PUSH_STATE.read_text(encoding="utf-8"))
        return st.get("date") == today and st.get("fingerprint") == fp
    except Exception:
        return False  # 状态缺失/损坏 → 当作未推 (宁可多推一次不丢推送)


def _evening_push_mark_sent(today: str, fp: str) -> None:
    try:
        _TRACK_PUSH_STATE.write_text(json.dumps({
            "date": today, "fingerprint": fp,
            "pushed_at": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except Exception as e:
        _logger.warning(f"[playbook] 盘后推送去重状态写失败 (下轮可能重推一次): {e}",
                        exc_info=True)


def run_playbook_track(
    scheduler,
    *,
    force: bool = False,
    today_provider: Callable[[], str],
    playbook_enabled: Callable[[], bool],
    trading_day_today: Callable[[], bool],
    run_ic_backfill_only: Callable,
    force_close_orphaned_sim_positions: Callable,
    now_et_provider: Callable,
    module_file: str,
    logger,
) -> Optional[Dict[str, Any]]:
    """
    盘后追踪 (Layer 3 每日盘后)。

    幂等: scheduler._playbook_tracked_date == today 即跳过 (force=True 可重跑)。
    """
    try:
        today = today_provider()

        if not playbook_enabled():
            logger.debug("[playbook] playbook.enabled=false, 跳过盘后追踪 (上线闸门关闭)")
            return None

        if not force:
            if not trading_day_today():
                logger.debug(f"[playbook] {today} 非交易日, 跳过盘后追踪")
                return None
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                instructions = db.get_instructions_by_date(today)

                if getattr(scheduler, "_playbook_tracked_date", None) == today:
                    run_ic_backfill_only(scheduler, today)
                    return None

                tracked = not instructions or all(
                    inst.get("outcome") in ("win", "loss", "skipped", "missed", "void")
                    for inst in instructions
                )
                if tracked:
                    scheduler._playbook_tracked_date = today
                    run_ic_backfill_only(scheduler, today)
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
        try:
            sim_closed = force_close_orphaned_sim_positions(scheduler)
            if sim_closed:
                logger.info(f"[playbook] EOD sim 强制平仓: {sim_closed} 个 sim 仓位已 broker 平仓")
        except Exception as _e:
            logger.warning(f"[playbook] EOD sim 强制平仓异常 (已隔离): {_e}")

        tracker.daily_tracking(today)

        try:
            from app.playbook.factor_analyzer import FactorAnalyzer
            updated = FactorAnalyzer(db).update_all_stats()
            logger.info(f"[playbook] 因子统计更新: {updated}")
        except Exception as e:
            logger.debug(f"[playbook] 因子更新失败 (不影响追踪): {e}")

        try:
            from app.playbook.horizon_return_model import HorizonReturnModel
            total_with_outcome = db._conn().execute(
                "SELECT COUNT(*) FROM playbook_instructions WHERE outcome IN ('win','loss')"
            ).fetchone()[0]
            if total_with_outcome >= 50:
                hrm_updated = HorizonReturnModel(db).update(days=90)
                logger.info(f"[playbook] Horizon 收益模型更新: {hrm_updated}")
            else:
                logger.debug(
                    f"[playbook] Horizon 收益模型跳过 (样本不足: {total_with_outcome} < 50)"
                )
        except Exception as e:
            logger.debug(f"[playbook] Horizon 收益模型更新失败 (不影响追踪): {e}")

        try:
            from app.playbook.ic_tracker import backfill_forward_returns, compute_playbook_ic, format_ic_report
            market_data = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
            if market_data:
                backfilled = backfill_forward_returns(db, market_data, today)
                logger.info(f"[playbook] IC 前瞻收益回填: {backfilled} 条")

                now_et = datetime.now(now_et_provider())
                if now_et.weekday() == 4:
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
            from app.core.feature_flags import FeatureFlags
            if FeatureFlags().is_enabled("unified_backfill"):
                from app.analysis.signal_ic_tracker import run_signal_ic_backfill
                signal_stats = run_signal_ic_backfill(dry_run=False)
                total_signal = sum(signal_stats.values())
                logger.info(f"[playbook] Signal IC 统一回填: {total_signal} 条, {len(signal_stats)} 只股票")
        except Exception as e:
            logger.debug(f"[playbook] Signal IC 统一回填失败 (不影响主流程): {e}")

        try:
            import subprocess, sys
            subprocess.run(
                [sys.executable, "scripts/update_signal_lifecycle.py"],
                cwd=str(Path(module_file).resolve().parent.parent.parent),
                timeout=60, capture_output=True,
            )
            logger.info(f"[playbook] signal_lifecycle 同步完成")
        except Exception as e:
            logger.debug(f"[playbook] signal_lifecycle 同步失败 (不影响主流程): {e}")

        try:
            # 持久去重 (跨重启): 内容指纹一致 ⇒ 跳过重推; 有变化 (新结算/改价) ⇒ 照推
            _fp = _evening_push_fingerprint(db, today)
            if not force and _evening_push_already_sent(db, today, _fp):
                logger.info(f"[playbook] 盘后追踪 {today} 已推送且内容无变化, 跳过重推")
            else:
                PlaybookPusher(db).push_evening_tracking(today)
                _evening_push_mark_sent(today, _fp)
        except Exception as e:
            logger.debug(f"[playbook] 盘后推送失败 (不影响追踪): {e}")

        scheduler._playbook_tracked_date = today
        return {"date": today, "tracked": True}

    except Exception as e:
        logger.exception(f"[playbook] 盘后追踪异常 (已隔离): {e}")
        return {"error": str(e)}


def run_playbook_weekly(
    scheduler,
    *,
    force: bool = False,
    now_et_provider: Callable,
    playbook_enabled: Callable[[], bool],
    playbook_cfg: Callable,
    today_provider: Callable[[], str],
    get_total_equity: Callable,
    logger,
) -> Optional[Dict[str, Any]]:
    """
    每周五: 双层因子分析 + 权重自动调整 + LLM 周报 + 飞书推送 (Layer 3 每周)。

    幂等: scheduler._playbook_weekly_done == 本周结束日 即跳过 (force=True 可重跑)。
    """
    try:
        now_et = datetime.now(now_et_provider())
        days_to_friday = (4 - now_et.weekday()) % 7
        week_end = (now_et.date() + timedelta(days=days_to_friday)).isoformat()

        if not playbook_enabled() or not playbook_cfg("enable_weekly", True):
            logger.debug("[playbook] playbook 周报闸门关闭, 跳过周度复盘")
            return None

        if not force:
            if getattr(scheduler, "_playbook_weekly_done", None) == week_end:
                logger.debug(f"[playbook] 本周 ({week_end}) 已出周报 (内存标志), 跳过")
                return None
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                has_weekly = db.has_weekly_report_marker(week_end)
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
        report = generator.generate(today_provider())

        generator.format_report(report)
        try:
            generator.push_report(report)
        except Exception as e:
            logger.debug(f"[playbook] 周报推送失败 (不影响分析): {e}")

        try:
            from app.decision.edge_weekly_report import push_edge_weekly_report
            push_edge_weekly_report(days=7)
        except Exception as e:
            logger.debug(f"[playbook] edge 周报推送失败 (不影响分析): {e}")

        try:
            from app.playbook.risk_circuit_breaker import RiskCircuitBreaker
            status = RiskCircuitBreaker(db).check_status(get_total_equity(scheduler))
            if not status.get("trading_allowed") or float(status.get("position_multiplier", 1.0)) < 1.0:
                logger.warning(f"[playbook] 风控熔断触发: {status}")
        except Exception as e:
            logger.debug(f"[playbook] 风控检查失败 (不影响周报): {e}")

        try:
            from app.playbook.feedback_learner import FeedbackLearner
            learner = FeedbackLearner(db)
            feedback_report = learner.learn_from_feedback(days=7)
            if feedback_report.get("feedback_count", 0) > 0:
                report["feedback_learning"] = feedback_report
                logger.info(f"[playbook] 反馈学习完成: {feedback_report['feedback_count']} 条反馈")
                screener_adj = feedback_report.get("screener_adjustments", {})
                if screener_adj:
                    logger.info(f"[playbook] Screener 调整建议: {screener_adj}")
                    report["screener_adjustments_suggested"] = screener_adj
        except Exception as e:
            logger.debug(f"[playbook] 反馈学习失败 (不影响周报): {e}")

        logger.info(f"[playbook] ✅ 周度复盘完成: {report.get('week', week_end)}")
        scheduler._playbook_weekly_done = week_end
        try:
            from app.utils.timezone import now_utc
            db.save_regime_log({
                "date": week_end,
                "regime": "weekly_report",
                "vix": 0,
                "spy_price": 0,
                "spy_ma20": 0,
                "spy_ma50": 0,
                "score": 0,
                "notes": f"weekly_report generated at {now_utc().isoformat()}",
            })
        except Exception as e:
            logger.debug(f"[playbook] DB 幂等标记写入失败: {e}")
        return report

    except Exception as e:
        logger.exception(f"[playbook] 周度复盘异常 (已隔离): {e}")
        return {"error": str(e)}
