"""
Harness Pilot — 港股 Harness 整车平台试点入口

渐进式包裹方案：不修改 main.py 的交易路径，只在系统启动后注入
Harness Coordinator 作为后台协调器，负责盘后复盘等非交易工作流。

设计原则：
1. 不触碰 _on_consensus 等热路径
2. 不阻塞 main() 的线性启动流程
3. 独立 event loop 运行（避免与 DataSubscriber 的 threading 冲突）
4. 所有 harness 组件通过 adapter 层接入，不直接依赖 DataSubscriber 内部状态
5. 【2026-05-02 新增】kill-switch: 配置 harness_enabled=false 一键关闭

使用方式：
    from app.harness_pilot import HarnessPilot

    # 在 main() 中 DataSubscriber 创建后注入
    pilot = HarnessPilot(
        data_subscriber=subscriber,
        risk_manager=risk_manager,
    )
    pilot.start()  # 启动后台协调器（独立线程 + event loop）
"""

import asyncio
import logging
import os
import threading
from datetime import datetime
from typing import Any, Optional

import pytz

logger = logging.getLogger("ftseek.harness_pilot")

__all__ = ["HarnessPilot"]


class HarnessPilot:
    """
    Harness 试点协调器。

    作为 main.py 之上的包裹层，提供：
    - 盘后 DailyReviewWorkflow
    - 周度 WeeklyReviewWorkflow
    - 手动触发诊断/审计任务
    """

    def __init__(
        self,
        data_subscriber: Any = None,
        risk_manager: Any = None,
        notifier: Any = None,
    ):
        """
        Args:
            data_subscriber: DataSubscriber 实例（用于访问持仓/趋势等数据）
            risk_manager: RiskManager 实例
            notifier: 通知推送器
        """
        # 【2026-05-02 Kill-Switch】一键关闭 Harness 所有功能
        self._enabled = os.getenv("FTSEEK_HARNESS_ENABLED", "true").lower() in ("true", "1", "yes")
        if not self._enabled:
            logger.warning("HarnessPilot DISABLED by FTSEEK_HARNESS_ENABLED=false")

        self.data_subscriber = data_subscriber
        self.risk_manager = risk_manager
        self.notifier = notifier
        self._coordinator = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._logger = logger
        # 防重: 记录每周已触发的事件
        self._weekly_meta_date: Optional[str] = None

    def start(self) -> None:
        """启动 Harness Pilot（独立线程 + event loop）"""
        if not self._enabled:
            self._logger.info("HarnessPilot start() skipped (disabled by kill-switch)")
            return
        if self._running:
            self._logger.warning("HarnessPilot already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="HarnessPilot",
            daemon=True,
        )
        self._thread.start()
        self._logger.info("HarnessPilot started (background thread)")

    def stop(self) -> None:
        """停止 Harness Pilot"""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._logger.info("HarnessPilot stopped")

    def run_daily_review(self, date: str = None, market: str = "HK") -> dict:
        """
        同步入口：触发当日复盘工作流。
        可从非 async 代码调用（如 FastAPI 路由、定时任务）。
        """
        if not self._enabled:
            return {"status": "skipped", "reason": "harness disabled"}
        if not self._loop or not self._loop.is_running():
            self._logger.warning("HarnessPilot not running, skipping daily review")
            return {"status": "skipped", "reason": "pilot not running"}

        future = asyncio.run_coroutine_threadsafe(
            self._execute_daily_review(date, market),
            self._loop,
        )
        try:
            return future.result(timeout=300)
        except Exception as e:
            self._logger.error(f"Daily review failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    def run_risk_check(self, portfolio: dict = None) -> dict:
        """同步入口：触发风控检查"""
        if not self._enabled:
            return {"status": "skipped", "reason": "harness disabled"}
        if not self._loop or not self._loop.is_running():
            return {"status": "skipped", "reason": "pilot not running"}

        future = asyncio.run_coroutine_threadsafe(
            self._execute_risk_check(portfolio),
            self._loop,
        )
        try:
            return future.result(timeout=60)
        except Exception as e:
            self._logger.error(f"Risk check failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    def run_weekly_meta_sync(self, date: str = None) -> dict:
        """
        同步入口: 触发周末元分析 (供 APScheduler 调用)。
        将 async 方法安全地投递到 HarnessPilot 的 event loop 上执行。
        """
        if not self._enabled:
            return {"status": "skipped", "reason": "harness disabled"}
        if not self._loop or not self._loop.is_running():
            self._logger.warning("HarnessPilot not running, skipping weekly meta analysis")
            return {"status": "skipped", "reason": "pilot not running"}

        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        future = asyncio.run_coroutine_threadsafe(
            self._execute_weekly_meta_analysis(date),
            self._loop,
        )
        try:
            return future.result(timeout=600)
        except Exception as e:
            self._logger.error(f"Weekly meta analysis failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_event_loop(self) -> None:
        """在独立线程中运行 event loop"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._setup_agents())
            self._loop.run_forever()
        except Exception as e:
            self._logger.error(f"HarnessPilot event loop crashed: {e}", exc_info=True)
        finally:
            self._loop.close()
            self._logger.info("HarnessPilot event loop closed")

    async def _setup_agents(self) -> None:
        """初始化 Coordinator 并注册 Agent"""
        from app.harness.coordinator import Coordinator
        from app.harness.messaging import get_message_bus

        self._coordinator = Coordinator(message_bus=get_message_bus())

        # 注册 CollectorAdapter（交易数据收集）
        try:
            from app.harness.adapters.collector_adapter import CollectorAdapter
            collector = CollectorAdapter()
            await collector._load_module("app.review.periodic_collector_hk")
            self._coordinator.register_agent("trade-monitor", collector)
            self._logger.info("CollectorAdapter registered")
        except Exception as e:
            self._logger.warning(f"CollectorAdapter registration failed: {e}")

        # 注册 RiskAdapter（风控评估）
        try:
            from app.harness.adapters.risk_adapter import RiskAdapter
            risk_adapter = RiskAdapter()
            if self.risk_manager:
                risk_adapter._risk_manager = self.risk_manager
            await risk_adapter._load_module("app.risk.risk_manager")
            self._coordinator.register_agent("risk-evaluator", risk_adapter)
            self._logger.info("RiskAdapter registered")
        except Exception as e:
            self._logger.warning(f"RiskAdapter registration failed: {e}")

        # 注册 ContextEngineer（记忆/归档）
        try:
            from app.harness.context_engineer import ContextEngineer
            engineer = ContextEngineer()
            self._coordinator.register_agent("context-engineer", engineer)
            self._logger.info("ContextEngineer registered")
        except Exception as e:
            self._logger.warning(f"ContextEngineer registration failed: {e}")

        # 注册 Strategist（策略分析）
        try:
            from app.harness.strategist import Strategist
            strategist = Strategist()
            self._coordinator.register_agent("strategy-gen", strategist)
            self._logger.info("Strategist registered")
        except Exception as e:
            self._logger.warning(f"Strategist registration failed: {e}")

        # 注册 HKEODReviewAdapter（全链路 EOD 复盘）
        try:
            from app.harness.adapters.review_adapter import HKEODReviewAdapter
            review_adapter = HKEODReviewAdapter(notifier=self.notifier)
            await review_adapter._load_module("app.review.hk_eod_review")
            self._coordinator.register_agent("hk-eod-review", review_adapter)
            self._logger.info("HKEODReviewAdapter registered")
        except Exception as e:
            self._logger.warning(f"HKEODReviewAdapter registration failed: {e}")

        # 注册 PremarkDebateAgent（盘前多模型辩论）
        try:
            from app.harness.premarket_debate import PremarkDebateAgent
            debate_agent = PremarkDebateAgent()
            self._coordinator.register_agent("premarket-debate", debate_agent)
            self._logger.info("PremarkDebateAgent registered")
        except Exception as e:
            self._logger.warning(f"PremarkDebateAgent registration failed: {e}")

        self._logger.info("HarnessPilot agents setup complete")

        # 启动心跳 (迁移自 _scheduling_loop → 直接调用)
        await self._send_startup_heartbeat()

    async def _execute_daily_review(self, date: str, market: str) -> dict:
        """执行盘后复盘"""
        from app.harness.workflows.daily_review import DailyReviewWorkflow

        if not self._coordinator:
            return {"status": "error", "reason": "coordinator not initialized"}

        workflow = DailyReviewWorkflow(coordinator=self._coordinator)
        ctx = await workflow.execute(date or "today", market)

        return {
            "status": ctx.status,
            "workflow_id": ctx.workflow_id,
            "task_results": {
                name: {
                    "success": r.success,
                    "execution_time": r.execution_time,
                }
                for name, r in ctx.task_results.items()
            },
        }

    async def _execute_risk_check(self, portfolio: dict) -> dict:
        """执行风控检查"""
        if not self._coordinator:
            return {"status": "error", "reason": "coordinator not initialized"}

        risk_adapter = self._coordinator._agents.get("risk-evaluator")
        if not risk_adapter:
            return {"status": "error", "reason": "risk-evaluator not registered"}

        result = await risk_adapter.run({
            "task": "assess",
            "portfolio": portfolio or {},
        })
        return result

    async def _execute_weekly_meta_analysis(self, date: str) -> dict:
        """周末执行系统元分析 + 知识进化"""
        try:
            # 1. 知识进化 (模式检测 + 置信度更新)
            from app.review.knowledge_accumulator import get_knowledge_accumulator
            acc = get_knowledge_accumulator()
            evolve_result = acc.weekly_evolve()
            self._logger.info(
                f"知识进化完成: {evolve_result.get('patterns_detected', 0)} 个模式, "
                f"summary={evolve_result.get('knowledge_summary', {})}"
            )

            # 2. 元分析
            from app.review.meta_analyst import run_meta_analysis_sync, generate_evolution_report
            meta = run_meta_analysis_sync()

            # 3. 生成进化报告 + 推送
            report = generate_evolution_report(meta, {
                "period": f"{date} (周度总结)",
                "trading_days": 5,
                "trades": {},
            })

            if self.notifier:
                try:
                    self.notifier.send_text(report)
                    self._logger.info("系统进化周报已推送")
                except Exception as e:
                    self._logger.warning(f"进化周报推送失败: {e}")

            # 4. 领域事件 — 元分析 + 知识进化
            from app.core.domain_events import get_event_bus, DomainEvent, EventType, EvolutionWeeklyPayload
            get_event_bus().emit(DomainEvent(
                type=EventType.EVOLUTION_WEEKLY,
                data=EvolutionWeeklyPayload(
                    date=date,
                    evolve_result=evolve_result,
                    meta_report=report,
                    meta_score=meta.weekly_score,
                )
            ))
            self._logger.info("进化周报领域事件已发射")

            self._logger.info(
                f"周末元分析完成: score={meta.weekly_score:.0f}, "
                f"adjustments={len(meta.strategy_adjustments)}, "
                f"rules={len(meta.new_system_rules)}"
            )
            return {"status": "completed", "meta": meta.to_dict()}
        except Exception as e:
            self._logger.error(f"周末元分析失败: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def _send_startup_heartbeat(self) -> None:
        """启动心跳 — 通知用户 FTSeek 各管线状态"""
        try:
            from app.shared.market_hours import is_trading_day, MarketType

            hkt = pytz.timezone("Asia/Hong_Kong")
            now = datetime.now(hkt)
            is_hk_trading = is_trading_day(MarketType.HK, now.date())

            # 检查磁盘剩余空间作为环境检测
            import shutil
            disk_usage = shutil.disk_usage("/opt/ftseek")

            lines = [
                "🫀 **FTSeek 系统心跳**",
                "",
                f"⏰ 启动时间: {now.strftime('%Y-%m-%d %H:%M')} HKT",
                f"📅 交易日: {'🟢 是' if is_hk_trading else '⚫ 否'}",
                "",
                "**活跃管线:**",
                "| 管线 | 状态 | 时段 |",
                "|------|------|------|",
                "| 多模型辩论 | ✅ APScheduler | 08:20 HKT |",
                "| 盘前作战计划 | ✅ APScheduler | 08:30/09:00 HKT |",
                "| 多Agent辩论 | ✅ APScheduler | 盘前自动触发 |",
                "| 午间刷新 | ✅ APScheduler | 12:00 HKT |",
                "| 午间归因 | ✅ APScheduler | 12:15 HKT |",
                "| 盘后EOD复盘 | ✅ APScheduler | 16:15 HKT |",
                "| LLM归因分析 | ✅ APScheduler | 盘后自动 |",
                "| 次日预判 | ✅ APScheduler | 盘后二阶推导 |",
                "| 知识积累 | ✅ APScheduler | 每日积累 |",
                "| 周末元分析 | ✅ APScheduler | 周六 10:00 |",
                "",
                f"💾 磁盘: {disk_usage.free // (1024**3)}GB 可用",
                "",
                "---",
                "*各阶段结果将自动推送到钉钉/飞书*",
            ]

            if self.notifier:
                self.notifier.send_text("\n".join(lines))
                self._logger.info("启动心跳已推送")
        except Exception as e:
            self._logger.warning(f"启动心跳推送失败 (非阻塞): {e}")
