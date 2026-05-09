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

        # P0 修复: 启动时检查是否错过调度时间，若错过则立即补执行一次
        self._catchup_if_missed()

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

    def _catchup_if_missed(self):
        """P0 修复: 启动时若错过所有调度时间，立即补执行一次"""
        if not self.enabled:
            return

        now = datetime.now()
        current_hm = now.strftime("%H:%M")

        # 检查今天是否已经运行过
        if self._last_run_at and datetime.fromtimestamp(self._last_run_at).date() == now.date():
            logger.info("[PreMarketScheduler] 今日已执行过，跳过补执行")
            return

        # 检查当前时间是否已经过了所有调度时间
        sorted_times = sorted(self.schedule_times)
        if current_hm > sorted_times[-1]:
            logger.info(
                f"[PreMarketScheduler] 错过调度时间 (当前{current_hm} > 最晚{sorted_times[-1]})，"
                f"立即补执行 Pipeline"
            )
            self._execute_pipeline()
        else:
            # 在调度时间之间，检查是否已经过了部分时间
            missed = [t for t in sorted_times if t < current_hm]
            if missed:
                logger.info(
                    f"[PreMarketScheduler] 错过 {missed}，等待下一次调度 {sorted_times}"
                )

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
        """发送 Pipeline 执行汇总报告到 DingTalk/飞书 并生成盘前作战计划"""
        try:
            # 发送管线执行摘要
            summary = self._format_summary(result)
            _send_pipeline_notification(summary, result)
            debate_synthesis = None
            try:
                from app.agents.debate.debate_orchestrator import run_debate_sync
                from app.review.hk_memory_injector import get_hk_memory_injector

                injector = get_hk_memory_injector()

                # P0 fix (2026-05-09): 使用 PipelineResult 上的 market_context/price_context
                # (现在 Pipeline 执行阶段已注入真实数据)
                assessments = getattr(result, 'assessments', []) or []
                event_packs = getattr(result, 'event_packs', None) or getattr(result, 'raw_news', [])

                # 构建上下文用于智能注入 — 优先使用 assessments
                if assessments:
                    context = _build_context_from_assessments(assessments)
                else:
                    context = _build_injection_context(event_packs if isinstance(event_packs, list) else [])
                feedback = injector.inject(context) if injector else ""
                # 使用 Pipeline 收集的真实市场数据
                market_ctx = getattr(result, 'market_context', '') or ''
                price_data = getattr(result, 'price_context', '') or ''

                logger.info("[PreMarketScheduler] 启动多Agent辩论...")

                # ── 双轨: 尝试 Hermes Agent 队列 ──
                _hermes_debate_result = None
                try:
                    cfg = self.pipeline_config.get('hermes_agent', {}) if self.pipeline_config else {}
                    premarket_hermes = cfg.get('premarket_enabled', False)
                    if not premarket_hermes:
                        from app.core.config_container import get_config_center
                        cc = get_config_center()
                        premarket_hermes = cc.get('hermes_agent', {}).get('premarket_enabled', False) if cc else False

                    if premarket_hermes:
                        from app.llm.agent_bridge import get_ftseek_agent_bridge
                        bridge = get_ftseek_agent_bridge("hk")
                        h_payload = {
                            "market_context": str(market_ctx),
                            "feedback_context": feedback,
                            "price_data": str(price_data),
                            "event_packs_count": len(event_packs) if isinstance(event_packs, list) else 0,
                        }
                        h_rid = bridge.enqueue_task("battle_plan", h_payload)
                        if h_rid:
                            logger.info("[PreMarketScheduler] Hermes 任务已入队: %s", h_rid)
                            _hermes_debate_result = bridge.wait_for_queue_result(h_rid, timeout=120)
                            if _hermes_debate_result:
                                logger.info("[PreMarketScheduler] Hermes 辩论完成: bias=%s",
                                           _hermes_debate_result.get('overall_bias', '?'))
                except Exception as _he:
                    logger.warning("[PreMarketScheduler] Hermes 路径异常 (降级): %s", _he)

                # 原有辩论 (Hermes 失败/未启用时的主路径)
                # P0 修复 (2026-05-05): 传入 assessments 而非空 event_packs
                if _hermes_debate_result is None:
                    debate_synthesis = run_debate_sync(
                        assessments=assessments if assessments else [],
                        market_context=str(market_ctx),
                        feedback_context=feedback,
                        price_data=str(price_data),
                    )
                else:
                    # 将 Hermes dict 结果适配为 DebateSynthesis 兼容对象
                    debate_synthesis = _hermes_to_debate_synthesis(_hermes_debate_result)
                logger.info(
                    f"[PreMarketScheduler] 辩论完成: "
                    f"共识={debate_synthesis.consensus_direction} "
                    f"conviction={debate_synthesis.conviction:.2f}"
                )
                # 推送辩论摘要到钉钉/飞书
                _send_debate_notification(debate_synthesis)
            except Exception as e:
                logger.warning(f"[PreMarketScheduler] 辩论执行失败 (非阻塞): {e}")

            # 🚀 自动生成盘前作战计划 (Battle Plan)
            if result and hasattr(result, "assessments") and result.assessments:
                from app.pipeline.battle_plan_generator import BattlePlanGenerator
                generator = BattlePlanGenerator()

                # P2 2026-05-06: 先加载多模型辩论结果 (08:20 调度器输出)
                generator.load_premarket_debate()

                if debate_synthesis:
                    generator.set_debate_synthesis(debate_synthesis)
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


def _build_context_from_assessments(assessments: List) -> Dict:
    """从 LLM 分析结果构建辩论上下文 (P0 fix 2026-05-05)"""
    sectors = set()
    symbols = set()
    directions = {"BUY": 0, "SELL": 0, "NO_TRADE": 0}
    high_conf = []
    key_events = []
    invalidating = []

    for a in assessments:
        d = getattr(a, "direction", "NO_TRADE") or "NO_TRADE"
        directions[d] = directions.get(d, 0) + 1

        conf = getattr(a, "confidence", 0) or 0
        syms = getattr(a, "focus_symbols", []) or []
        for s in syms:
            symbols.add(s)
            if conf >= 0.6:
                high_conf.append({"symbol": s, "direction": d, "confidence": conf,
                                  "reason": getattr(a, "second_order_insight", "")[:100]})

        events = getattr(a, "key_events", []) or []
        key_events.extend(events[:3])

        inv = getattr(a, "invalidating_factors", []) or []
        invalidating.extend(inv[:2])

        scene = getattr(a, "primary_scene", "") or ""
        if scene:
            sectors.add(scene)

    return {
        "sectors": list(sectors),
        "symbols": list(symbols),
        "directions": directions,
        "high_confidence_targets": high_conf[:10],
        "key_events": list(set(key_events))[:10],
        "invalidating_factors": list(set(invalidating))[:5],
        "total_assessments": len(assessments),
    }


def _build_injection_context(event_packs: List) -> Dict:
    """从事件包提取上下文 (sectors, symbols, keywords)"""
    sectors = set()
    symbols = set()
    keywords = set()

    for pack in (event_packs or [])[:20]:
        industry = getattr(pack, "industry", "") or ""
        if industry:
            sectors.add(industry)

        focus = getattr(pack, "focus_symbols", []) or []
        for s in focus:
            symbols.add(s)

        title = getattr(pack, "title", "") or getattr(pack, "headline", "") or ""
        body = getattr(pack, "content", "") or getattr(pack, "summary", "") or ""
        # 提取关键词
        for text in [title, body]:
            for kw in ["科技", "医药", "地产", "金融", "能源", "消费", "互联网",
                       "利率", "政策", "关税", "南向", "外资", "盈利", "估值",
                       "加息", "降息", "通胀", "GDP", "PMI"]:
                if kw in str(text):
                    keywords.add(kw.lower())

    return {
        "sectors": list(sectors),
        "symbols": list(symbols),
        "keywords": list(keywords),
    }


def _send_pipeline_notification(summary: str, result: Any) -> None:
    """推送盘前管线执行摘要到钉钉/飞书"""
    try:
        from app.notifier.unified import get_unified_notifier
        notifier = get_unified_notifier()
        if notifier:
            notifier.send_text(summary)
            logger.info("[管线通知] 已推送到钉钉/飞书")
    except Exception as e:
        logger.warning(f"[管线通知] 推送失败 (非阻塞): {e}")


def _send_debate_notification(synthesis) -> None:
    """推送辩论摘要到钉钉/飞书"""
    try:
        from app.notifier.unified import get_unified_notifier

        direction = synthesis.consensus_direction
        conviction = synthesis.conviction
        quality = synthesis.debate_quality_score
        risk = synthesis.risk_consensus

        dir_emoji = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "⚪"
        risk_emoji = "🟢" if risk == "LOW" else "🟡" if risk == "MEDIUM" else "🔴"

        lines = [
            "🧠 **多Agent辩论完成**",
            "",
            f"共识方向: {dir_emoji} **{direction}** (conviction={conviction:.2f})",
            f"风险共识: {risk_emoji} **{risk}**",
            f"辩论质量: **{quality:.2f}**",
            "",
        ]

        # 各Agent最终观点
        opinions = synthesis.agent_opinions
        if opinions:
            lines.append("**Agent 观点:**")
            for op in opinions:
                name = op.get("agent", "?")
                d = op.get("direction", "?")
                conf = op.get("confidence", 0)
                symbols = op.get("top_symbols", [])[:3]
                sym_str = ", ".join(s.replace("HK.", "") for s in symbols) if symbols else "—"
                agree = op.get("agree_with", [])
                disagree = op.get("disagree_with", [])

                lines.append(f"- **{name}**: {d} (conf={conf:.2f}) → {sym_str}")
                if agree:
                    lines.append(f"  - 同意: {'; '.join(agree[:2])}")
                if disagree:
                    lines.append(f"  - 反对: {'; '.join(disagree[:2])}")

        # 核心分歧
        if synthesis.disagreement_flags:
            lines.extend([
                "",
                "**⚡ 核心分歧:**",
            ])
            for flag in synthesis.disagreement_flags[:3]:
                lines.append(f"- {flag}")

        # 推荐标的
        if synthesis.merged_symbols:
            lines.extend([
                "",
                "**推荐标的:**",
            ])
            for s in synthesis.merged_symbols[:5]:
                sym = s.get("symbol", "").replace("HK.", "")
                sup = s.get("support", "")
                conc = s.get("concern", "")
                lines.append(f"- {sym}: {sup}" + (f" (⚠️{conc})" if conc else ""))

        # 辩论总结
        if synthesis.summary:
            lines.extend([
                "",
                f"> {synthesis.summary[:200]}",
            ])

        lines.extend([
            "",
            "---",
            "*辩论结果已注入作战计划 · 盘前自动触发*",
        ])

        notifier = get_unified_notifier()
        if notifier:
            notifier.send_text("\n".join(lines))
            logger.info("[辩论通知] 已推送到钉钉/飞书")
        else:
            logger.warning("[辩论通知] notifier 不可用，跳过推送")

        # 领域事件 — 辩论合成
        from app.core.domain_events import get_event_bus, DomainEvent, EventType, PlanDebatePayload
        from datetime import date
        debate_md = "\n".join(lines)
        get_event_bus().emit(DomainEvent(
            type=EventType.PLAN_DEBATE,
            data=PlanDebatePayload(
                date=str(date.today()),
                markdown=debate_md,
            )
        ))
        logger.info("[辩论] 领域事件已发射")
    except Exception as e:
        logger.warning(f"[辩论通知] 推送失败 (非阻塞): {e}")


def _hermes_to_debate_synthesis(hermes_result: Dict) -> Any:
    """将 Hermes Agent 的 battle_plan 结果适配为 DebateSynthesis 兼容对象"""
    from collections import namedtuple
    Synthesis = namedtuple("HermesDebateSynthesis", [
        "consensus_direction", "conviction", "debate_quality_score",
        "risk_consensus", "agent_opinions", "disagreement_flags",
        "merged_symbols", "summary",
    ])

    bias = hermes_result.get("overall_bias", "NEUTRAL")
    targets = hermes_result.get("targets", [])
    confidence = max((t.get("confidence", 0.5) for t in targets), default=0.5) if targets else 0.5
    warnings = hermes_result.get("risk_warnings", [])

    merged = [
        {
            "symbol": t.get("symbol", ""),
            "direction": t.get("direction", "LONG"),
            "support": t.get("rationale", ""),
            "concern": "; ".join(warnings[:2]) if warnings else "",
        }
        for t in targets[:5]
    ]

    return Synthesis(
        consensus_direction=bias,
        conviction=confidence,
        debate_quality_score=0.7,
        risk_consensus="MEDIUM" if warnings else "LOW",
        agent_opinions=[{"agent": "HermesAgent", "direction": bias, "confidence": confidence}],
        disagreement_flags=warnings[:3],
        merged_symbols=merged,
        summary=hermes_result.get("summary", ""),
    )
