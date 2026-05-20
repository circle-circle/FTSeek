"""
港股 EOD 复盘编排器 (HKEODReview)

职责:
1. 盘后 16:15 HKT 触发
2. 编排 Collector → AttributionRunner → MemoryInjector → Notifier
3. 生成标准化模块化复盘报告 (可独立更新各模块)
4. 推送钉钉通知

模块化报告结构 (每个模块独立方法, 可单独迭代):
  M1: 盈亏总览        M2: 逐笔交易明细    M3: 交易质量分析
  M4: 作战计划执行     M5: 止损/止盈复盘    M6: 时间阶段分布
  M7: 异常提醒         M8: A股联动         M9: 经验教训
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.review.hk_eod_review")


class HKEODReview:
    """港股 EOD 复盘编排器 — 标准化模块化报告"""

    def __init__(self, quote_ctx=None, notifier=None):
        self._quote_ctx = quote_ctx
        self._notifier = notifier
        self._collector = None
        self._attribution = None
        self._memory_injector = None

    # ════════════════════════════════════════════════════════════════
    #  主流程
    # ════════════════════════════════════════════════════════════════

    def run(self, trade_date: Optional[str] = None) -> Dict:
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        logger.info(f"[HKEODReview] === EOD 复盘开始 ({trade_date}) ===")

        result = {
            "trade_date": trade_date, "status": "running",
            "trades_reviewed": 0, "lessons_generated": 0,
            "memory_updated": False, "notification_sent": False,
            "anomaly_count": 0, "errors": [],
        }

        # Step 1: 收集
        try:
            self._collector = self._get_collector()
            collected = self._collector.collect(trade_date)
            result["trades_reviewed"] = len(collected.get("trades", []))
            result["anomaly_count"] = len(collected.get("anomalies", []))
            logger.info(f"[HKEODReview] Step 1: {result['trades_reviewed']}笔交易, {result['anomaly_count']}个异常")
        except Exception as e:
            logger.error(f"[HKEODReview] Step 1 失败: {e}", exc_info=True)
            result["errors"].append(f"collector: {e}")
            return result

        # Step 1b: 加载午间上下文 (从 feedback relay)
        try:
            from app.review.hk_feedback_relay import load_midday_context
            midday = load_midday_context()
            if midday:
                collected["midday_context"] = midday
                logger.info(f"[HKEODReview] Step 1b: 已加载午间上下文 ({len(midday.get('new_targets', []))} 只标的)")
        except Exception as _midday_err:
            logger.debug(f"[HKEODReview] Step 1b 午间上下文加载失败 (非阻塞): {_midday_err}")

        # Step 2: 归因
        lessons = []
        try:
            self._attribution = self._get_attribution()
            lessons = self._attribution.run(collected)
            result["lessons_generated"] = len(lessons)
            logger.info(f"[HKEODReview] Step 2: {len(lessons)}条教训")
        except Exception as e:
            logger.error(f"[HKEODReview] Step 2 失败: {e}", exc_info=True)
            result["errors"].append(f"attribution: {e}")

        # Step 3: 复盘DB
        try:
            from app.review.review_db_writer import ReviewDBWriter
            result["review_db"] = ReviewDBWriter().populate_all(date=trade_date)
            logger.info(f"[HKEODReview] Step 3: 复盘DB已填充")
        except Exception as e:
            logger.error(f"[HKEODReview] Step 3 失败: {e}", exc_info=True)
            result["errors"].append(f"review_db: {e}")

        # Step 4: 记忆 + 知识存储 (P1 2026-05-06)
        if lessons:
            try:
                self._memory_injector = self._get_memory_injector()
                self._memory_injector.update_memory(lessons)
                result["memory_updated"] = True
                logger.info(f"[HKEODReview] Step 4: 记忆已更新")

                # 知识沉淀 — 将教训和复盘数据存入 Knowledge Store
                try:
                    from app.review.knowledge_store import get_knowledge_store
                    ks = get_knowledge_store()

                    for lesson in lessons:
                        ks.save_lesson(
                            date=trade_date,
                            category=lesson.get('category', 'general'),
                            title=lesson.get('title', ''),
                            content=lesson.get('content', ''),
                            stock_code=lesson.get('stock_code'),
                            pnl_impact=lesson.get('pnl_impact'),
                            confidence=lesson.get('confidence'),
                            tags=lesson.get('tags', []),
                        )

                    # 存储 EOD 复盘摘要
                    trades = collected.get("trades", [])
                    if trades:
                        total_pnl = sum(self._pnl(t) for t in trades)
                        win_count = sum(1 for t in trades if self._pnl(t) > 0)
                        win_rate = win_count / max(len(trades), 1)

                        ks.save_eod_summary(
                            date=trade_date,
                            total_trades=len(trades),
                            win_rate=win_rate,
                            total_pnl=total_pnl,
                            lessons_count=len(lessons),
                            regime="auto",
                        )

                except Exception as _ke:
                    logger.warning(f"[HKEODReview] 知识存储失败 (非阻塞): {_ke}")

            except Exception as e:
                logger.error(f"[HKEODReview] Step 4 失败: {e}", exc_info=True)
                result["errors"].append(f"memory: {e}")

        # Step 4b: 模式发现 + 假设生成 (P1 2026-05-10) — 从历史交易中挖掘可执行规律
        try:
            from app.review.pattern_miner import get_pattern_miner
            miner = get_pattern_miner()
            patterns = miner.mine(days=60)
            if patterns:
                miner.save_to_knowledge_db(patterns)
                result["patterns_discovered"] = len(patterns)
                logger.info(f"[HKEODReview] Step 4b: 发现 {len(patterns)} 个可执行模式")

                # ★ 假设驱动进化：从模式生成可测试假设（对标 RD-Agent）
                try:
                    from app.review.hypothesis_engine import get_hypothesis_engine
                    engine = get_hypothesis_engine()
                    hypotheses = engine.generate_from_patterns(patterns)
                    result["hypotheses_generated"] = len(hypotheses)

                    # 输出进化状态摘要
                    summary = engine.get_evolution_summary()
                    result["evolution_summary"] = summary
                    logger.info(f"[HKEODReview] 假设进化状态: {summary}")

                    # 将已确认规则写入反馈文件
                    confirmed_rules = summary.get('confirmed_rules', [])
                    if confirmed_rules:
                        try:
                            from app.review.hk_feedback_relay import save_eod_feedback
                            save_eod_feedback({
                                "trade_date": trade_date,
                                "confirmed_rules": confirmed_rules,
                                "hypotheses": [h.to_dict() for h in hypotheses],
                            })
                            logger.info(f"[HKEODReview] {len(confirmed_rules)} 条确认规则已写入反馈")
                        except Exception as _fb_e:
                            logger.debug(f"[HKEODReview] 反馈写入失败: {_fb_e}")
                except Exception as _hyp_e:
                    logger.warning(f"[HKEODReview] 假设引擎初始化失败 (非阻塞): {_hyp_e}")

                # 将 HIGH severity 模式写入反馈文件，供次日盘前使用
                high_patterns = [p for p in patterns if p.get('severity') == 'HIGH']
                if high_patterns:
                    try:
                        from app.review.hk_feedback_relay import save_eod_feedback
                        save_eod_feedback({
                            "trade_date": trade_date,
                            "high_patterns": high_patterns,
                        })
                        logger.info(f"[HKEODReview] {len(high_patterns)} 个高危模式已写入反馈")
                    except Exception as _fb_e:
                        logger.debug(f"[HKEODReview] 反馈写入失败: {_fb_e}")
            else:
                result["patterns_discovered"] = 0
        except Exception as _pm_e:
            logger.warning(f"[HKEODReview] Step 4b 模式发现失败 (非阻塞): {_pm_e}")

        # Step 5: 次日预判 (盘后二阶推导 — 双轨: Hermes / 内部)
        next_day_preview = None
        try:
            # ── 双轨: 尝试 Hermes Agent ──
            _hermes_eod_result = None
            try:
                from app.core.config_container import get_config_center
                cc = get_config_center()
                if cc and cc.get('hermes_agent', {}).get('eod_enabled', False):
                    from app.llm.agent_bridge import get_ftseek_agent_bridge
                    bridge = get_ftseek_agent_bridge("hk")
                    eod_payload = {
                        "trades_context": json.dumps(collected.get("trades", []), ensure_ascii=False, default=str),
                        "ashare_data": json.dumps(collected.get("ashare_context", {}), ensure_ascii=False, default=str),
                    }
                    eod_rid = bridge.enqueue_task("eod_review", eod_payload)
                    if eod_rid:
                        logger.info("[HKEODReview] Hermes EOD 任务已入队: %s", eod_rid)
                        _hermes_eod_result = bridge.wait_for_queue_result(eod_rid, timeout=120)
                        if _hermes_eod_result:
                            logger.info("[HKEODReview] Hermes EOD 完成")
            except Exception as _he:
                logger.warning("[HKEODReview] Hermes EOD 路径异常 (降级): %s", _he)

            # 原有路径 (Hermes 失败/未启用时)
            if _hermes_eod_result is None:
                from app.pipeline.next_day_preview import generate_next_day_preview_sync
                plan_gap = None
                if self._attribution:
                    try:
                        plan_gap = self._attribution.get_plan_gap_analysis()
                    except Exception:
                        pass
                next_day_preview = generate_next_day_preview_sync(
                    collected, lessons, plan_gap,
                )
            else:
                # 用 Hermes 结果构建 NextDayPreview
                from app.pipeline.next_day_preview import NextDayPreview
                ndp = _hermes_eod_result.get("next_day_preview", {})
                next_day_preview = NextDayPreview(
                    regime_shift_flags=_hermes_eod_result.get("regime_shift_flags", []),
                    sector_rotation=[],
                    watchlist_changes=[],
                    emergent_risks=_hermes_eod_result.get("emergent_risks", []),
                    overnight_watch=ndp.get("overnight_watch", []),
                    market_bias_next_day=ndp.get("bias", "NEUTRAL"),
                    key_question=ndp.get("key_question", ""),
                    raw_response=json.dumps(_hermes_eod_result, ensure_ascii=False),
                )
            if next_day_preview and (
                next_day_preview.regime_shift_flags or
                next_day_preview.emergent_risks or
                next_day_preview.watchlist_changes
            ):
                self._inject_next_day_preview(next_day_preview)
                result["next_day_preview"] = next_day_preview.to_dict()
                logger.info(
                    f"[HKEODReview] Step 5: 次日预判完成 "
                    f"(regime_shifts={len(next_day_preview.regime_shift_flags)}, "
                    f"risks={len(next_day_preview.emergent_risks)})"
                )

                # P1 2026-05-06: 存储预判到 Knowledge Store
                try:
                    from app.review.knowledge_store import get_knowledge_store
                    ks = get_knowledge_store()
                    ks.save_prediction(
                        date=trade_date,
                        bias=next_day_preview.market_bias_next_day,
                        key_question=next_day_preview.key_question,
                        risks=[r.get('description', str(r)) for r in next_day_preview.emergent_risks[:5]],
                        watchlist=[w.get('symbol', str(w)) for w in next_day_preview.overnight_watch[:10]],
                    )
                except Exception as _ks:
                    logger.debug(f"[HKEODReview] 预判存储失败: {_ks}")
                # 领域事件 — 次日预判
                from app.core.domain_events import get_event_bus, DomainEvent, EventType, ReviewNextDayPayload
                get_event_bus().emit(DomainEvent(
                    type=EventType.REVIEW_NEXTDAY,
                    data=ReviewNextDayPayload(trade_date=trade_date, preview_text=next_day_preview.to_injection_text())
                ))
        except Exception as e:
            logger.warning(f"[HKEODReview] Step 5 次日预判失败 (非阻塞): {e}")

        # Step 6: 生成报告 (模块化 — 包含次日预判)
        try:
            result["summary"] = self._generate_summary(
                collected, lessons, self._attribution, next_day_preview,
            )
            logger.info(f"[HKEODReview] Step 6: 报告已生成")
        except Exception as e:
            logger.error(f"[HKEODReview] Step 6 失败: {e}", exc_info=True)
            result["errors"].append(f"summary: {e}")

        # Step 6b: 领域事件 — 盘后复盘
        from app.core.domain_events import get_event_bus, DomainEvent, EventType, ReviewEODPayload
        get_event_bus().emit(DomainEvent(
            type=EventType.REVIEW_EOD,
            data=ReviewEODPayload(
                trade_date=trade_date,
                market='HK',
                trades_reviewed=result['trades_reviewed'],
                win_rate=len([t for t in collected.get('trades', []) if self._pnl(t) > 0]) / max(result['trades_reviewed'], 1),
                total_pnl=sum(self._pnl(t) for t in collected.get('trades', [])),
                max_drawdown=collected.get('capital_state', {}).get('max_drawdown', 0),
                trades=[{'code': t['code'], 'pnl': self._pnl(t), 'id': str(i)}
                           for i, t in enumerate(collected.get('trades', []))],
                summary=result.get('summary', ''),
                lessons=lessons,
            )
        ))
        logger.info(f"[HKEODReview] Step 6b: 盘后复盘领域事件已发射")

        # Step 7: 推送
        if result.get("summary"):
            try:
                self._send_notification(result["summary"])
                result["notification_sent"] = True
                logger.info(f"[HKEODReview] Step 7: 通知已推送")
            except Exception as e:
                logger.error(f"[HKEODReview] Step 7 失败: {e}", exc_info=True)
                result["errors"].append(f"notification: {e}")

        result["status"] = "completed"
        logger.info(f"[HKEODReview] === EOD 复盘完成: {result['trades_reviewed']}笔, {len(result['errors'])}个错误 ===")

        # Step 8: 保存反馈到 relay 文件 (连接次日盘前)
        try:
            from app.review.hk_feedback_relay import save_eod_feedback
            next_day_text = ""
            if next_day_preview and hasattr(next_day_preview, 'to_injection_text'):
                next_day_text = next_day_preview.to_injection_text()
            elif isinstance(next_day_preview, str):
                next_day_text = next_day_preview
            save_eod_feedback(
                trade_date=trade_date,
                summary=result.get("summary", {}),
                next_day_preview=next_day_text,
                lessons=lessons,
                battle_plan_execution=collected.get("battle_plan", {}),
                trades=collected.get("trades", []),
            )
            logger.info(f"[HKEODReview] Step 8: EOD 反馈已保存到 relay")
        except Exception as _relay_err:
            logger.warning(f"[HKEODReview] Step 8 反馈保存失败 (非阻塞): {_relay_err}")

        return result

    # ════════════════════════════════════════════════════════════════
    #  组件懒加载
    # ════════════════════════════════════════════════════════════════

    def _get_collector(self):
        from app.review.hk_eod_collector import HKEODCollector
        return HKEODCollector(quote_ctx=self._quote_ctx)

    def _get_attribution(self):
        from app.review.hk_attribution_runner import HKAttributionRunner
        return HKAttributionRunner()

    def _get_memory_injector(self):
        from app.review.hk_memory_injector import get_hk_memory_injector
        injector = get_hk_memory_injector()
        injector.ensure_memory_file_exists()
        return injector

    def _inject_next_day_preview(self, preview) -> None:
        """将次日预判注入记忆系统"""
        try:
            injector = self._get_memory_injector()
            injection_text = preview.to_injection_text()
            injector.inject_next_day_preview(injection_text)
            logger.info("[HKEODReview] 次日预判已注入记忆系统")
        except Exception as e:
            logger.warning(f"[HKEODReview] 注入次日预判失败: {e}")

    def _send_notification(self, summary: str) -> None:
        """推送 EOD 复盘通知 (notifier 优先, 否则 fallback 到 DingTalk)"""
        try:
            if self._notifier:
                self._notifier.send_text(summary)
                logger.info("[HKEODReview] 通知已通过注入 notifier 推送")
                return
            from app.risk.dingtalk_alert import init_dingtalk_alert
            from app import config
            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET,
            )
            sender.send_text(summary, alert_level="INFO")
        except Exception as e:
            logger.warning(f"[HKEODReview] 推送失败: {e}")

    # ════════════════════════════════════════════════════════════════
    #  报告生成器 (模块化 — 每个 section 独立可迭代)
    # ════════════════════════════════════════════════════════════════

    def _generate_summary(self, collected: Dict, lessons: List[Dict],
                          attribution=None, next_day_preview=None) -> str:
        """组装所有报告模块"""
        sections = [
            self._section_header(collected),
            self._section_pnl_overview(collected),
            self._section_trade_details(collected),
            self._section_quality_analysis(collected),
            self._section_battle_plan(collected),
            self._section_stop_loss_review(collected),
            self._section_time_phases(collected),
            self._section_anomalies(collected),
            self._section_ashare_context(collected),
            self._section_plan_gap(attribution),
            self._section_lessons(lessons),
            self._section_next_day_preview(next_day_preview),
            self._section_footer(),
        ]
        return "\n".join(s for s in sections if s)

    # ── 工具函数 ──────────────────────────────────────────────────

    @staticmethod
    def _pnl(t: Dict) -> float:
        return t.get("pnl", t.get("realized_pnl", 0)) or 0

    @staticmethod
    def _code(t: Dict) -> str:
        return t.get("code", "").replace("HK.", "")

    @staticmethod
    def _qty(t: Dict) -> int:
        return int(t.get("quantity", 0) or 0)

    @staticmethod
    def _entry_px(t: Dict) -> float:
        return float(t.get("entry_price", 0) or 0)

    @staticmethod
    def _exit_px(t: Dict) -> float:
        return float(t.get("exit_price", 0) or 0)

    @staticmethod
    def _parse_ts(ts: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(ts.replace("T", " "))
        except Exception:
            return None

    @staticmethod
    def _format_hold(entry: str, exit_s: str) -> str:
        et = HKEODReview._parse_ts(entry)
        xt = HKEODReview._parse_ts(exit_s)
        if not et or not xt:
            return "—"
        delta = xt - et
        if delta.days > 0:
            return f"{delta.days}d{delta.seconds//3600}h"
        if delta.seconds >= 3600:
            return f"{delta.seconds//3600}h{(delta.seconds%3600)//60}m"
        return f"{delta.seconds//60}m"

    # ── M0: 报告头部 ──────────────────────────────────────────────

    def _section_header(self, collected: Dict) -> str:
        date = collected.get("trade_date", "")
        return f"📊 **港股盘后复盘报告**\n📅 {date}\n\n---\n"

    # ── M1: 盈亏总览 ──────────────────────────────────────────────

    def _section_pnl_overview(self, collected: Dict) -> str:
        trades = collected.get("trades", [])
        capital = collected.get("capital_state", {})
        anomalies = collected.get("anomalies", [])

        total = len(trades)
        if total == 0:
            return "## 💰 盈亏总览\n\n⚠️ 今日无交易记录\n"

        wins = [t for t in trades if self._pnl(t) > 0]
        losses = [t for t in trades if self._pnl(t) < 0]
        total_pnl = capital.get("today_realized_pnl", 0)
        wr = len(wins) / total * 100

        lines = [
            "## 💰 盈亏总览",
            "",
            f"- 今日交易: **{total} 笔** (✅{len(wins)}赢 / ❌{len(losses)}亏)",
            f"- 胜率: **{wr:.0f}%**",
            f"- 今日盈亏: **{total_pnl:+,.2f} HKD**",
            f"- 未实现盈亏: {capital.get('unrealized_pnl', 0):+,.2f}",
            f"- 当前持仓: {capital.get('open_position_count', 0)} 个",
            f"- 异常提醒: {len(anomalies)} 项",
            f"",
        ]
        return "\n".join(lines)

    # ── M2: 逐笔交易明细 ──────────────────────────────────────────

    def _section_trade_details(self, collected: Dict) -> str:
        trades = collected.get("trades", [])
        if not trades:
            return ""

        lines = [
            "## 📋 逐笔交易明细",
            "",
            "| 标的 | 方向 | 数量 | 入场 | 出场 | 盈亏 | 盈亏% | 决策依据 | 持仓 |",
            "|------|------|------|------|------|------|-------|----------|------|",
        ]

        for t in trades:
            code = self._code(t)
            action = t.get("action", "?")
            qty = self._qty(t)
            entry_px = self._entry_px(t)
            exit_px = self._exit_px(t)
            pnl = self._pnl(t)
            pnl_pct = (pnl / (entry_px * qty) * 100) if entry_px and qty else 0
            hold = self._format_hold(t.get("entry_time", ""), t.get("exit_time", ""))

            # 决策依据
            sig = t.get("signal_summary", {}) or {}
            dec_reason = sig.get("decision_reason", "")
            regime = sig.get("market_regime", "")
            ai = sig.get("ai_sentiment", 0)
            decision = f"{dec_reason}(AI{ai:.2f})" if dec_reason and ai else dec_reason or "手动同步"

            emoji = "🟢" if pnl > 0 else "🔴"
            lines.append(
                f"| {emoji}{code} | {action} | {qty:,} | {entry_px:.2f} | {exit_px:.2f} "
                f"| {pnl:+,.2f} | {pnl_pct:+.2f}% | {decision} | {hold} |"
            )

        return "\n".join(lines) + "\n"

    # ── M3: 交易质量分析 ──────────────────────────────────────────

    def _section_quality_analysis(self, collected: Dict) -> str:
        trades = collected.get("trades", [])
        if not trades:
            return ""

        wins = [t for t in trades if self._pnl(t) > 0]
        losses = [t for t in trades if self._pnl(t) < 0]

        if not wins and not losses:
            return ""

        best = max(trades, key=self._pnl)
        worst = min(trades, key=self._pnl)
        avg_win = sum(self._pnl(t) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(self._pnl(t) for t in losses) / len(losses) if losses else 0
        total_win = sum(self._pnl(t) for t in wins)
        total_loss = abs(sum(self._pnl(t) for t in losses))
        pf = (total_win / total_loss) if total_loss > 0 else float("inf")

        # 按信号评分分组
        with_sig = [t for t in trades if t.get("signal_summary")]
        if with_sig:
            avg_sig = sum(t.get("signal_summary", {}).get("signal_score", 0) for t in with_sig) / len(with_sig)
            avg_ai = sum(t.get("signal_summary", {}).get("ai_sentiment", 0) for t in with_sig) / len(with_sig)

        lines = [
            "## 📈 交易质量分析",
            "",
            f"- 最佳: **{self._code(best)}** {self._pnl(best):+,.2f}",
            f"- 最差: **{self._code(worst)}** {self._pnl(worst):+,.2f}",
            f"- 平均盈利: {avg_win:+,.2f}  |  平均亏损: {avg_loss:+,.2f}",
            f"- 盈亏比 (Profit Factor): **{pf:.2f}**",
            f"- 总盈利: {total_win:+,.2f}  |  总亏损: {total_loss:,.2f}",
        ]
        if with_sig:
            lines.append(f"- 平均信号分: {avg_sig:.2f}  |  平均AI情绪: {avg_ai:.2f}")
        lines.append("")
        return "\n".join(lines)

    # ── M4: 作战计划执行分析 ──────────────────────────────────────

    def _section_battle_plan(self, collected: Dict) -> str:
        plan = collected.get("battle_plan") or {}
        plan_targets = plan.get("targets", [])
        if not plan_targets:
            return "## 🎯 作战计划执行\n\n⚠️ 今日无作战计划\n"

        trades = collected.get("trades", [])
        plan_symbols = {t["symbol"] for t in plan_targets}
        traded_symbols = {self._code(t) for t in trades if "HK." + self._code(t) in plan_symbols}
        # 修复: 用完整 HK. 前缀匹配
        traded_full = {t.get("code", "") for t in trades}
        planned_traded = traded_full & plan_symbols
        planned_untraded = plan_symbols - traded_full
        off_plan = traded_full - plan_symbols

        plan_pnl = sum(self._pnl(t) for t in trades if t.get("code") in plan_symbols)
        off_pnl = sum(self._pnl(t) for t in trades if t.get("code") not in plan_symbols)

        hit_rate = len(planned_traded) / max(len(plan_targets), 1) * 100

        lines = [
            "## 🎯 作战计划执行分析",
            "",
            f"- 计划目标: **{len(plan_targets)}** 个",
            f"- 实际交易: **{len(traded_full)}** 个标的",
            f"- 计划命中: **{len(planned_traded)}** 个 (命中率 {hit_rate:.0f}%)",
            f"- 计划内盈亏: {plan_pnl:+,.2f}",
        ]

        if planned_traded:
            lines.append(f"- ✅ 已执行: {', '.join(s.replace('HK.','') for s in sorted(planned_traded))}")
        if off_plan:
            lines.append(f"- ⚠️ 计划外: {', '.join(s.replace('HK.','') for s in sorted(off_plan))}, 盈亏{off_pnl:+,.2f}")
        if planned_untraded and len(planned_untraded) <= 8:
            lines.append(f"- ⬜ 未触发: {', '.join(s.replace('HK.','') for s in sorted(planned_untraded))}")
        elif planned_untraded:
            lines.append(f"- ⬜ 未触发: {len(planned_untraded)} 个标的")

        lines.extend([
            "",
            f"- 计划倾向: {plan.get('overall_bias','N/A')}  |  市场状态: {plan.get('market_regime','N/A')}  |  风险: {plan.get('risk_level','N/A')}",
            f"- 生成方式: {plan.get('reason','N/A')[:50]}",
            f"",
        ])
        return "\n".join(lines)

    # ── M5: 止损/止盈复盘 ─────────────────────────────────────────

    def _section_stop_loss_review(self, collected: Dict) -> str:
        trades = collected.get("trades", [])
        if not trades:
            return ""

        stops = [t for t in trades if t.get("exit_analysis", {}).get("type") == "止损"]
        profits = [t for t in trades if t.get("exit_analysis", {}).get("type") == "止盈"]
        manuals = [t for t in trades if t.get("exit_analysis", {}).get("type") not in ("止损", "止盈")]

        lines = [
            "## 🛑 止损/止盈复盘",
            "",
            f"- 止损平仓: **{len(stops)}** 笔",
            f"- 止盈平仓: **{len(profits)}** 笔",
            f"- 主动平仓: **{len(manuals)}** 笔",
            "",
            "| 类型 | 标的 | 详情 | 质量 |",
            "|------|------|------|------|",
        ]

        for t in trades:
            ea = t.get("exit_analysis", {}) or {}
            lines.append(
                f"| {ea.get('type','?')} | {self._code(t)} | {ea.get('detail','')} | {ea.get('quality','')} |"
            )

        if any(t.get("exit_analysis", {}).get("quality", "").startswith("⚠️") for t in trades):
            lines.append("")
            lines.append("> ⚠️ 存在出场质量问题，详见上方标注")

        lines.append("")
        return "\n".join(lines)

    # ── M6: 时间阶段分布 ──────────────────────────────────────────

    def _section_time_phases(self, collected: Dict) -> str:
        trades = collected.get("trades", [])
        if not trades:
            return ""

        from collections import Counter
        entry_phases = Counter(t.get("time_phase", {}).get("entry_phase", "?") for t in trades)
        exit_phases = Counter(t.get("time_phase", {}).get("exit_phase", "?") for t in trades)

        lines = [
            "## ⏱️ 时间阶段分布",
            "",
            "**入场阶段:**",
        ]
        for phase in ["盘前", "开盘冲刺", "早盘", "午休", "午后", "尾盘前", "尾盘"]:
            cnt = entry_phases.get(phase, 0)
            if cnt:
                pnl_sum = sum(self._pnl(t) for t in trades if t.get("time_phase", {}).get("entry_phase") == phase)
                lines.append(f"- {phase}: {cnt}笔 (盈亏{pnl_sum:+,.0f})")

        lines.extend(["", "**出场阶段:**"])
        for phase in ["开盘冲刺", "早盘", "午休", "午后", "尾盘前", "尾盘"]:
            cnt = exit_phases.get(phase, 0)
            if cnt:
                lines.append(f"- {phase}: {cnt}笔")

        # 持仓时长统计
        holds = [t.get("time_phase", {}).get("hold_minutes", 0) or 0 for t in trades]
        if holds:
            avg_hold = sum(holds) / len(holds)
            lines.extend([
                "",
                f"- 平均持仓: **{avg_hold:.0f}分钟**",
                f"- 最短: {min(holds)}分钟  |  最长: {max(holds)}分钟",
            ])

        intraday = sum(1 for t in trades if t.get("is_intraday"))
        lines.append(f"- 日内交易(T+0): {intraday}/{len(trades)}笔")
        lines.append("")
        return "\n".join(lines)

    # ── M7: 异常提醒 ──────────────────────────────────────────────

    def _section_anomalies(self, collected: Dict) -> str:
        anomalies = collected.get("anomalies", [])
        trades = collected.get("trades", [])

        lines = ["## 🚨 异常提醒", ""]

        if not anomalies and trades:
            lines.append("✅ 未检测到异常模式")
            lines.append("")
            return "\n".join(lines)

        if not trades:
            lines.append("— 今日无交易")
            lines.append("")
            return "\n".join(lines)

        for a in anomalies:
            lines.append(f"{a.get('level','⚠️')} **{a.get('type','')}**: {a.get('msg','')}")
            sug = a.get("suggestion", "")
            if sug:
                lines.append(f"  → {sug}")

        lines.append("")
        return "\n".join(lines)

    # ── M8: A股联动 ───────────────────────────────────────────────

    def _section_ashare_context(self, collected: Dict) -> str:
        ashare = collected.get("ashare_context")
        if not ashare:
            return ""

        name_map = {
            "SH.000001": "上证指数", "SH.000300": "沪深300",
            "SZ.399006": "创业板指", "SH.000016": "上证50",
        }

        lines = ["## 📈 A股收盘", ""]
        for code, data in ashare.items():
            change = data.get("change_pct", 0)
            name = name_map.get(code, code)
            emoji = "🔴" if change < 0 else "🟢"
            lines.append(f"- {emoji} {name}: {data.get('last_price', 0):.2f} ({change:+.2f}%)")
        lines.append("")
        return "\n".join(lines)

    # ── M9: 经验教训 ──────────────────────────────────────────────

    def _section_lessons(self, lessons: List[Dict]) -> str:
        if not lessons:
            return ""

        lines = ["## 🧠 经验教训", ""]
        for i, lesson in enumerate(lessons[:8]):
            symbol = lesson.get("symbol", "?")
            pnl_pct = lesson.get("pnl_percent", 0)
            text = lesson.get("lesson", "")
            cat = lesson.get("cause", "")
            emoji = "🔴" if (pnl_pct or 0) < -2 else "🟡" if (pnl_pct or 0) < 0 else "🟢"

            # 翻译 cause 类别
            cause_map = {
                "direction_wrong": "方向错误", "timing_poor": "时机不佳",
                "market_misread": "市场误判", "signal_noise": "信号噪音",
                "reasonable_loss": "合理亏损", "stop_loss_triggered": "止损触发",
                "execution_poor": "执行偏差", "risk_control_failed": "风控失效",
                "successful_trade": "成功交易", "intraday_chase_high": "追高",
                "intraday_panic_sell": "恐慌卖出", "ashare_divergence": "AH背离",
                "execution_deviation": "执行偏离", "plan_missing": "计划缺失",
                "manual_override": "手动干预", "unknown": "待分析",
            }
            cat_cn = cause_map.get(cat, cat)
            lines.append(f"{i+1}. {emoji} **{symbol}** ({pnl_pct:+.1f}%) [{cat_cn}]: {text}")

            # LLM 归因详情 (如有)
            llm_attr = lesson.get("llm_attribution")
            if llm_attr:
                d = llm_attr.get("direction", {})
                e = llm_attr.get("execution", {})
                if d.get("root_cause"):
                    lines.append(f"   - 方向分析: {d['root_cause']}")
                if d.get("improvement"):
                    lines.append(f"     → {d['improvement']}")
                if e.get("root_cause") and e.get("cause_category") != "execution_aligned":
                    lines.append(f"   - 执行分析: {e['root_cause']}")
                    if e.get("improvement"):
                        lines.append(f"     → {e['improvement']}")

        lines.extend([
            "",
            "> 教训持续累积，用于后续作战计划优化与策略迭代",
            "",
        ])
        return "\n".join(lines)

    # ── M9.5: 计划差距分析 ─────────────────────────────────────────

    def _section_plan_gap(self, attribution) -> str:
        """计划 vs 执行差距分析 (LLM 驱动)"""
        gap = None
        try:
            gap = getattr(attribution, 'get_plan_gap_analysis', lambda: None)()
        except Exception:
            pass

        if not gap:
            return ""

        lines = ["## 📊 计划执行差距分析", ""]

        planned_traded = gap.get("planned_and_traded", [])
        planned_missed = gap.get("planned_but_missed", [])
        off_plan = gap.get("traded_off_plan", [])
        quality = gap.get("plan_quality_assessment", "N/A")
        root = gap.get("gap_root_cause", "")
        suggestion = gap.get("suggestion", "")

        if quality == "GOOD":
            lines.append(f"计划质量: 🟢 **{quality}**")
        elif quality == "FAIR":
            lines.append(f"计划质量: 🟡 **{quality}**")
        else:
            lines.append(f"计划质量: 🔴 **{quality}**")

        if planned_traded:
            lines.append(f"- ✅ 已执行计划标的: {', '.join(planned_traded)}")
        if planned_missed:
            lines.append(f"- ⬜ 计划但未交易:")
            for item in planned_missed:
                lines.append(f"  - {item}")
        if off_plan:
            lines.append(f"- ⚠️ 计划外交易:")
            for item in off_plan:
                lines.append(f"  - {item}")

        if root:
            lines.extend(["", f"**根因**: {root}", ""])
        if suggestion:
            lines.extend([f"**建议**: {suggestion}", ""])

        return "\n".join(lines)

    # ── M10: 次日预判 (盘后二阶推导) ──────────────────────────────

    def _section_next_day_preview(self, preview) -> str:
        """次日预判报告模块"""
        if not preview:
            return ""

        lines = ["## 🔮 次日预判 (盘后二阶推导)", ""]

        if preview.market_bias_next_day and preview.market_bias_next_day != "NEUTRAL":
            bias_emoji = "🟢" if preview.market_bias_next_day == "BUY" else "🔴"
            lines.append(f"明日倾向: {bias_emoji} **{preview.market_bias_next_day}**")

        if preview.regime_shift_flags:
            lines.append("**市场状态变化:**")
            for flag in preview.regime_shift_flags:
                lines.append(f"- {flag}")

        if preview.sector_rotation:
            lines.append("**板块轮动:**")
            for sr in preview.sector_rotation:
                frm = sr.get("from", "?")
                to = sr.get("to", "?")
                strength = sr.get("strength", "")
                lines.append(f"- {frm} → {to} ({strength})")

        if preview.watchlist_changes:
            lines.append("**Watchlist 调整建议:**")
            for wc in preview.watchlist_changes[:5]:
                symbol = wc.get("symbol", "?").replace("HK.", "")
                action = wc.get("action", "?")
                reason = wc.get("reason", "")
                act_map = {"elevate": "⬆️ 提升", "demote": "⬇️ 降级", "add": "➕ 新增", "remove": "➖ 移除"}
                act_cn = act_map.get(action, action)
                lines.append(f"- {act_cn} **{symbol}**: {reason}")

        if preview.emergent_risks:
            lines.append("**新识别风险:**")
            for risk in preview.emergent_risks:
                lines.append(f"- ⚠️ {risk}")

        if preview.overnight_watch:
            lines.append(f"**隔夜关注**: {', '.join(preview.overnight_watch)}")

        if preview.key_question:
            lines.extend(["", f"> ❓ **明日核心问题**: {preview.key_question}", ""])

        lines.append("")
        return "\n".join(lines)

    # ── M10: 报告尾部 ─────────────────────────────────────────────

    def _section_footer(self) -> str:
        now = datetime.now().strftime("%H:%M:%S")
        return f"---\n*FTSeek HK EOD Review v2.0 · 模块化模板 · {now}*"
