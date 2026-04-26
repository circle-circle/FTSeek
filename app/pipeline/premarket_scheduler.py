"""盘前 Pipeline 调度模块

在 PRE_MARKET_PLANNING 时段 (08:30-09:30 HKT) 自动触发 Pipeline 执行。

调度策略:
  - 08:30 HKT: 第一次运行 (捕捉隔夜新闻)
  - 09:00 HKT: 第二次运行 (更新最新变化)

支持:
  - 定时触发 (后台线程)
  - 手动触发 (API 调用)
  - Session 集成 (与 SessionManager 联动)
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Dict, Optional, List

logger = logging.getLogger("ftseek.pipeline.premarket_scheduler")


class PreMarketScheduler:
    """盘前 Pipeline 调度器

    在指定时间点自动触发 PreMarketPipeline 执行。
    """

    def __init__(
        self,
        pipeline_config: Optional[Dict] = None,
        schedule_times: Optional[List[str]] = None,
        timezone: str = "Asia/Hong_Kong",
    ):
        self.pipeline_config = pipeline_config or {}
        self.enabled = self.pipeline_config.get("enabled", True)

        # 调度时间 (HKT)
        self.schedule_times = schedule_times or ["08:30", "09:00"]
        self.timezone = self.pipeline_config.get("timezone", timezone)

        # 后台线程
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Pipeline 实例
        self._pipeline = None
        self._last_result = None
        self._last_run_at: Optional[float] = None

        logger.info(
            f"[PreMarketScheduler] 初始化完成: "
            f"enabled={self.enabled}, schedule={self.schedule_times}, tz={self.timezone}"
        )

    def start(self):
        """启动调度器 (后台线程)"""
        if not self.enabled:
            logger.info("[PreMarketScheduler] 未启用，跳过启动")
            return

        if self._thread and self._thread.is_alive():
            logger.warning("[PreMarketScheduler] 已在运行中")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="premarket-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("[PreMarketScheduler] 后台线程已启动")

    def stop(self):
        """停止调度器"""
        if not self._thread:
            return

        self._stop_event.set()
        self._thread.join(timeout=10)
        logger.info("[PreMarketScheduler] 后台线程已停止")

    def run_once(self) -> Any:
        """手动触发一次 Pipeline 执行

        Returns:
            PipelineResult 或 None
        """
        if not self.enabled:
            logger.warning("[PreMarketScheduler] 未启用，拒绝手动触发")
            return None

        logger.info("[PreMarketScheduler] 手动触发 Pipeline")
        return self._execute_pipeline()

    def get_last_result(self) -> Optional[Any]:
        """获取最近一次执行结果"""
        return self._last_result

    def get_status(self) -> Dict:
        """获取调度器状态"""
        return {
            "enabled": self.enabled,
            "running": self._thread.is_alive() if self._thread else False,
            "schedule_times": self.schedule_times,
            "last_run_at": (
                datetime.fromtimestamp(self._last_run_at).isoformat()
                if self._last_run_at
                else None
            ),
            "last_result": self._last_result.to_dict() if self._last_result else None,
        }

    # ── 内部方法 ─────────────────────────────────────────────────

    def _run_loop(self):
        """后台调度循环"""
        logger.info("[PreMarketScheduler] 调度循环启动")

        while not self._stop_event.is_set():
            now = datetime.now()
            current_hm = now.strftime("%H:%M")

            # 检查是否到达调度时间
            if current_hm in self.schedule_times:
                # 避免同一分钟内重复执行
                if self._last_run_at and (now.timestamp() - self._last_run_at) < 55:
                    time.sleep(10)
                    continue

                logger.info(
                    f"[PreMarketScheduler] 到达调度时间: {current_hm} "
                    f"(last_run={self._last_run_at})"
                )
                self._execute_pipeline()

            # 检查 session 状态
            if not self._in_premarket_session():
                # 不在盘前时段, 减少轮询频率
                time.sleep(60)
            else:
                # 在盘前时段, 高频轮询
                time.sleep(10)

        logger.info("[PreMarketScheduler] 调度循环退出")

    def _in_premarket_session(self) -> bool:
        """判断是否在盘前时段 (08:00-09:30 HKT)，且非节假日"""
        now = datetime.now()
        # 使用本地时间近似 HKT (服务器通常在 CST/UTC+8)
        if not (dtime(8, 0) <= now.time() <= dtime(9, 30)):
            return False

        # P2 修复 (2026-04-19): 节假日检查
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            from datetime import date as _date
            cal = get_holiday_calendar()
            today = _date(now.year, now.month, now.day)
            if cal.is_hk_holiday(today):
                logger.info(f"[PreMarketScheduler] 今日港股节假日 ({today})，跳过盘前")
                return False
        except Exception as e:
            logger.debug(f"[PreMarketScheduler] 节假日检查异常 (继续): {e}")

        return True

    def _execute_pipeline(self) -> Any:
        """执行 Pipeline"""
        self._last_run_at = time.time()

        try:
            from app.pipeline.orchestrator import PreMarketPipeline

            if self._pipeline is None:
                self._pipeline = PreMarketPipeline(config=self.pipeline_config)

            # 运行异步 Pipeline
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self._pipeline.run())
            finally:
                loop.close()

            self._last_result = result
            logger.info(
                f"[PreMarketScheduler] Pipeline 执行完成: "
                f"trace_id={result.trace_id}, 耗时={result.duration_seconds:.1f}s"
            )

            # 发送汇总报告
            self._send_summary_report(result)

            return result

        except Exception as e:
            logger.error(f"[PreMarketScheduler] Pipeline 执行异常: {e}", exc_info=True)
            return None

    def _send_summary_report(self, result: Any):
        """发送 Pipeline 执行汇总报告到 DingTalk 并生成盘前作战计划"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier

            # 尝试获取 notifier
            notifier = None
            try:
                from app.main import get_notifier
                notifier = get_notifier()
            except Exception:
                pass

            if notifier and result and hasattr(notifier, "send_text"):
                summary = self._format_summary(result)
                notifier.send_text(summary, title="盘前Pipeline执行报告")
            else:
                logger.info(
                    f"[PreMarketScheduler] Pipeline 汇总: "
                    f"新闻={result.raw_news_count}->{result.cleaned_news_count}, "
                    f"聚类={result.cluster_count}, "
                    f"分析={result.assessment_count}, "
                    f"路由: auto={result.auto_signals} review={result.review_cards} observe={result.observe_only}"
                )
            
            # 🚀 自动生成盘前作战计划 (Battle Plan)
            if result and hasattr(result, "assessments") and result.assessments:
                from app.pipeline.battle_plan_generator import BattlePlanGenerator
                generator = BattlePlanGenerator()
                generator.generate_from_assessments(result.assessments)

        except Exception as e:
            logger.error(f"[PreMarketScheduler] 汇总报告或计划生成失败: {e}")

    def _format_summary(self, result: Any) -> str:
        """格式化执行汇总"""
        lines = [
            f"盘前 Pipeline 执行报告",
            f"Trace ID: {result.trace_id}",
            f"耗时: {result.duration_seconds:.1f}s",
            f"",
            f"新闻: {result.raw_news_count} 原始 → {result.cleaned_news_count} 清洗后",
            f"事件聚类: {result.cluster_count} 个 ({result.relevant_cluster_count} 相关)",
            f"事件包: {result.pack_count} 个",
            f"LLM 分析: {result.assessment_count} 个",
            f"",
            f"路由结果:",
            f"  自动执行: {result.auto_signals}",
            f"  人工确认: {result.review_cards}",
            f"  仅观察: {result.observe_only}",
        ]

        if result.errors:
            lines.append("")
            lines.append(f"错误: {', '.join(result.errors)}")

        # 添加评估摘要
        if result.assessments:
            lines.append("")
            lines.append("分析摘要:")
            for a in result.assessments[:5]:  # 最多显示 5 个
                lines.append(
                    f"  [{a.direction}] {', '.join(a.focus_symbols[:2])} "
                    f"conf={a.confidence:.2f} rel={a.relevance_score:.2f} "
                    f"→ {a.recommendation[:40]}"
                )

        return "\n".join(lines)
