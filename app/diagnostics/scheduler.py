"""
诊断调度器 — FTSeek-US 诊断 Overlay 调度

非交易路径调度层，在盘后/非交易时段异步触发诊断模块。

零侵入设计:
  - 不修改 scheduler_tasks.py 或任何交易管线代码
  - 在 TradingScheduler 主循环外作为独立任务注册
  - 通过 unified_config.yaml 的 diagnostics.enabled 门控

集成方式:
  在 main.py 或 TradingScheduler 初始化后调用:
    from app.diagnostics.scheduler import register_diagnostics
    register_diagnostics(scheduler)
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class DiagnosticScheduler:
    """
    诊断任务调度器

    独立线程，不阻塞任何交易逻辑。
    按调度时段触发对应的诊断模块。
    """

    def __init__(self, config=None, notifier=None):
        self.config = config
        self.notifier = notifier
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._check_interval = 3600  # 每小时检查一次是否需要运行

    def _is_enabled(self) -> bool:
        """检查诊断功能是否启用"""
        if self.config is None:
            try:
                from app.core.config_container import get_config
                self.config = get_config()
            except Exception:
                return False

        return self.config.get_dotted("diagnostics.enabled", False)

    def _get_schedule(self) -> dict:
        """获取调度配置"""
        return self.config.get_dotted("diagnostics.schedule", {
            "post_market": {
                "enabled": True,
                "time": "20:30",  # 美东时间
                "modules": ["shadow_account", "statistical_validator"],
            },
            "on_demand": {
                "enabled": True,
                "modules": ["geopolitical_reasoner"],
            },
        })

    def start(self):
        """启动诊断调度器"""
        if not self._is_enabled():
            logger.info("[DiagnosticScheduler] 诊断功能未启用 (diagnostics.enabled=false)")
            return

        if self._running:
            logger.warning("[DiagnosticScheduler] 已在运行中")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="DiagnosticScheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[DiagnosticScheduler] 已启动 (daemon thread, 检查间隔={self._check_interval}s)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("[DiagnosticScheduler] 已停止")

    def run_once(self, schedule_type: str = "post_market", **kwargs):
        """
        立即执行一次指定时段的诊断

        可用于手动触发:
          scheduler.run_once("post_market")
        """
        logger.info(f"[DiagnosticScheduler] 手动触发 {schedule_type} 诊断...")
        from app.diagnostics import run_all_diagnostics

        results = run_all_diagnostics(schedule=schedule_type, **kwargs)
        self._notify_results(results, schedule_type)
        return results

    def _run_loop(self):
        """诊断调度主循环"""
        logger.info("[DiagnosticScheduler] 调度循环启动")

        while self._running:
            try:
                now = datetime.now()
                schedule = self._get_schedule()

                # 检查 post_market 时段
                post_cfg = schedule.get("post_market", {})
                if post_cfg.get("enabled"):
                    trigger_time = post_cfg.get("time", "20:30")
                    if self._should_trigger(trigger_time, last_run_attr="_last_post_market"):
                        self._execute_diagnostics("post_market", post_cfg)

                # 检查 geopolitical (on_demand)
                geo_cfg = schedule.get("on_demand", {})
                if geo_cfg.get("enabled"):
                    # 检查是否有新新闻
                    new_news = self._check_new_news()
                    if new_news:
                        self._execute_diagnostics("on_demand", geo_cfg, news_items=new_news)

                time.sleep(self._check_interval)

            except Exception as e:
                logger.error(f"[DiagnosticScheduler] 循环异常: {e}", exc_info=True)
                time.sleep(self._check_interval * 2)

    def _should_trigger(self, time_str: str, last_run_attr: str = "_last_run") -> bool:
        """检查是否应该触发 (避免重复执行)"""
        import re
        match = re.match(r"(\d{1,2}):(\d{2})", time_str)
        if not match:
            return False

        trigger_h, trigger_m = int(match.group(1)), int(match.group(2))
        now = datetime.now()

        if now.hour != trigger_h or now.minute < trigger_m:
            return False

        # 检查是否已经在这个时段执行过
        last_run = getattr(self, last_run_attr, None)
        if last_run and last_run.date() == now.date():
            return False

        return True

    def _execute_diagnostics(self, schedule_type: str, cfg: dict, **kwargs):
        """执行诊断模块"""
        from app.diagnostics import get_diagnostic

        modules = cfg.get("modules", [])
        db_path = kwargs.pop("db_path", None)

        if db_path is None:
            try:
                from app.core.config_container import get_config
                cc = get_config()
                db_path = cc.get_dotted("database.path", "data/ftseek_us.db")
            except Exception:
                db_path = "data/ftseek_us.db"

        results = {}
        for mod_name in modules:
            try:
                diag = get_diagnostic(mod_name)
                result = diag.execute(db_path=db_path, **kwargs)
                results[mod_name] = result
                logger.info(f"[DiagnosticScheduler] {mod_name} → {result.status}")
            except Exception as e:
                logger.error(f"[DiagnosticScheduler] {mod_name} 执行失败: {e}")

        self._notify_results(results, schedule_type)

        # 标记已执行
        setattr(self, f"_last_{schedule_type}", datetime.now())

    def _notify_results(self, results: dict, schedule_type: str):
        """推送诊断结果到通知渠道"""
        if not self.notifier:
            return

        summaries = []
        for name, result in results.items():
            if result.status == "success" and result.summary:
                summaries.append(result.summary)
            elif result.status == "error":
                summaries.append(f"❌ {name}: {result.summary}")

        if not summaries:
            return

        message = f"📋 诊断报告 ({schedule_type})\n" + "\n".join(summaries)

        try:
            self.notifier.send(message)
            logger.debug(f"[DiagnosticScheduler] 诊断结果已推送")
        except Exception as e:
            logger.error(f"[DiagnosticScheduler] 推送失败: {e}")

    def _check_new_news(self):
        """检查是否有新新闻 (用于地缘推理触发)"""
        try:
            from app.news_v2.pipeline import get_news_pipeline
            pipeline = get_news_pipeline()
            # 获取最近 1 小时内的新闻
            recent = pipeline.get_recent(hours=1)
            return recent if recent else []
        except Exception:
            return []


def register_diagnostics(scheduler=None, notifier=None):
    """
    注册诊断调度器到 TradingScheduler

    用法 (在 main.py 中):
      from app.diagnostics.scheduler import register_diagnostics
      register_diagnostics(scheduler, notifier)
    """
    diag_scheduler = DiagnosticScheduler(notifier=notifier)

    # 如果传入了 scheduler，绑定到 scheduler 的生命周期
    if scheduler:
        # 添加手动触发接口
        scheduler.run_diagnostics = diag_scheduler.run_once

        # 尝试在 scheduler 启动时同时启动诊断调度
        original_start = getattr(scheduler, 'start', None)
        if original_start:
            def patched_start(*args, **kwargs):
                result = original_start(*args, **kwargs)
                diag_scheduler.start()
                return result
            scheduler.start = patched_start

        original_stop = getattr(scheduler, 'stop', None)
        if original_stop:
            def patched_stop(*args, **kwargs):
                diag_scheduler.stop()
                return original_stop(*args, **kwargs)
            scheduler.stop = patched_stop

    return diag_scheduler
