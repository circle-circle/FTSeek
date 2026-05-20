#!/usr/bin/env python3
"""
FTSeek Obsidian 知识库插件 (2026-05-05)

纯插件 — 订阅领域事件，写入 Obsidian vault。
核心管线完全不知道此插件的存在，可随时启用/禁用。

事件映射:
  trade.opened           → trades/{date}_{code}_{id}.md
  trade.closed           → trades/{date}_{code}_{id}.md
  plan.premarket         → daily/premarket/{date}_hk_premarket.md
  plan.debate            → decisions/premarket/{date}_debate_synthesis.md
  plan.midday_refreshed  → daily/{date}_midday_refresh.md (v2)
  review.eod             → daily/postmarket/{date}_hk_postmarket.md + daily/{date}_eod_full_report.md + lessons/*.md
  review.nextday         → daily/{date}_next_day_preview.md
  review.midday          → daily/{date}_midday_review.md
  evolution.weekly       → attribution/{date}_knowledge_evolution.md + strategies/{date}_meta_analysis.md
  system.risk_adjusted   → risk/adjustments/{date}_risk_adjustment.md (v2)
"""

import json as _json
import logging
from datetime import date as _date

from app.core.domain_events import (
    DomainEvent,
    DomainEventBus,
    EventType,
    # v2 payload dataclasses — imported for documentation and future type-safe access
    TradeOpenedPayload,
    TradeClosedPayload,
    PlanPremarketPayload,
    PlanDebatePayload,
    PlanMiddayRefreshedPayload,
    ReviewEODPayload,
    ReviewNextDayPayload,
    ReviewMiddayPayload,
    EvolutionWeeklyPayload,
    RiskAdjustedPayload,
)
from app.core.obsidian_writer import ObsidianWriter, get_obsidian_writer

logger = logging.getLogger("ftseek.plugins.obsidian")


class ObsidianPlugin:
    """Obsidian 写入插件 — 事件驱动，零侵入"""

    def __init__(self, vault_path: str = None, enabled: bool = True):
        self.writer: ObsidianWriter = get_obsidian_writer(vault_path)
        self.enabled = enabled

    def register(self, bus: DomainEventBus) -> None:
        if not self.enabled:
            logger.info("[ObsidianPlugin] 已禁用，跳过注册")
            return
        bus.subscribe(EventType.TRADE_OPENED, self._on_trade_opened)
        bus.subscribe(EventType.TRADE_CLOSED, self._on_trade_closed)
        bus.subscribe(EventType.PLAN_PREMARKET, self._on_plan_premarket)
        bus.subscribe(EventType.PLAN_DEBATE, self._on_plan_debate)
        bus.subscribe(EventType.PLAN_MIDDAY_REFRESHED, self._on_plan_midday_refreshed)
        bus.subscribe(EventType.REVIEW_EOD, self._on_review_eod)
        bus.subscribe(EventType.REVIEW_NEXTDAY, self._on_review_nextday)
        bus.subscribe(EventType.REVIEW_MIDDAY, self._on_review_midday)
        bus.subscribe(EventType.EVOLUTION_WEEKLY, self._on_evolution_weekly)
        bus.subscribe(EventType.SYSTEM_RISK_ADJUSTED, self._on_system_risk_adjusted)
        logger.info("[ObsidianPlugin] 已注册 10 个事件监听器")

    # ── trade.opened ──────────────────────────────────────────────

    def _on_trade_opened(self, event: DomainEvent) -> None:
        d = event.data
        code = d.get("code", "UNKNOWN")
        # Use correlation_id for linked note IDs when available
        trade_id = event.correlation_id or f"OPEN-{code}-{event.timestamp.replace(':', '').replace('-', '').replace('T', '')}"
        self.writer.write_trade({
            "id": trade_id,
            "code": code,
            "market": d.get("market", "HK"),
            "entry_time": d.get("entry_time", event.timestamp),
            "exit_time": None,
            "entry_price": d.get("price", 0) or 0,
            "exit_price": None,
            "quantity": d.get("quantity", 0) or 0,
            "pnl": 0, "pnl_pct": 0,
            "strategy": d.get("regime", "consensus"),
            "action": d.get("action", "BUY"),
            "exit_reason": None,
            "trace_id": d.get("signal_trace_id", ""),
            "signal_id": d.get("signal_id", ""),
            "correlation_id": event.correlation_id,  # v2: for cross-note linking
        })

    # ── trade.closed ──────────────────────────────────────────────

    def _on_trade_closed(self, event: DomainEvent) -> None:
        d = event.data
        code = d.get("code", "UNKNOWN")
        exit_reason = d.get("exit_reason", "normal")
        prefix = "SL" if exit_reason == "stop_loss" else ("TP" if exit_reason in ("take_profit", "partial_take_profit") else "CLOSE")
        # Use correlation_id for linked note IDs when available
        trade_id = event.correlation_id or f"{prefix}-{code}-{event.timestamp.replace(':', '').replace('-', '').replace('T', '')}"
        self.writer.write_trade({
            "id": trade_id,
            "code": code,
            "market": d.get("market", "HK"),
            "entry_time": d.get("entry_time", ""),
            "exit_time": d.get("exit_time", event.timestamp),
            "entry_price": d.get("entry_price", 0) or 0,
            "exit_price": d.get("exit_price", 0) or 0,
            "quantity": d.get("quantity", 0) or 0,
            "pnl": d.get("pnl", 0) or 0.0,
            "pnl_pct": d.get("pnl_pct", 0) or 0,
            "strategy": d.get("strategy", "unknown"),
            "action": d.get("action", "unknown"),
            "exit_reason": exit_reason,
            "tags": d.get("tags", []),
            "trace_id": d.get("signal_trace_id", ""),
            "signal_id": d.get("signal_id", ""),
            "stop_loss": d.get("stop_loss", None),
            "take_profit": d.get("take_profit", None),
            "highest_price": d.get("highest_price", None),
            "lowest_price": d.get("lowest_price", None),
            "correlation_id": event.correlation_id,  # v2: for cross-note linking
        })

    # ── plan.premarket ────────────────────────────────────────────

    def _on_plan_premarket(self, event: DomainEvent) -> None:
        d = event.data
        self.writer.write_daily_premarket({
            "date": d.get("date", str(_date.today())),
            "market": d.get("market", "HK"),
            "targets": d.get("targets", []),
            "restricted": d.get("restricted", []),
            "overall_bias": d.get("overall_bias", "\u4e2d\u6027"),
            "regime": d.get("regime", "unknown"),
            "generated_at": d.get("generated_at", ""),
        })

    # ── plan.debate ───────────────────────────────────────────────

    def _on_plan_debate(self, event: DomainEvent) -> None:
        d = event.data
        debate_md = d.get("markdown", "")
        if not debate_md:
            return
        self.writer._write_md(
            "decisions/premarket",
            f"{d.get('date', str(_date.today()))}_debate_synthesis.md",
            debate_md,
            overwrite=True,
        )

    # ── plan.midday_refreshed (v2) ─────────────────────────────────

    def _on_plan_midday_refreshed(self, event: DomainEvent) -> None:
        """午间计划刷新 → 写入 daily/{date}_midday_refresh.md"""
        d = event.data
        refresh_date = d.get("date", str(_date.today()))
        added_count = d.get("added_count", 0)
        total_count = d.get("total_count", 0)
        new_targets = d.get("new_targets", [])
        ashare_context = d.get("ashare_context", [])

        # Build targets table
        targets_md = ""
        if new_targets:
            targets_md = "## 新增标的\n\n"
            for t in new_targets:
                targets_md += f"- {t}\n"
        else:
            targets_md = "## 新增标的\n\n- 无新增\n"

        # Build A-share context section
        ashare_md = ""
        if ashare_context:
            ashare_md = "## A股联动背景\n\n"
            for item in ashare_context:
                if isinstance(item, dict):
                    sector = item.get("sector", item.get("板块", ""))
                    trend = item.get("trend", item.get("走势", ""))
                    note = item.get("note", item.get("备注", ""))
                    ashare_md += f"- **{sector}**: {trend}"
                    if note:
                        ashare_md += f" ({note})"
                    ashare_md += "\n"
                else:
                    ashare_md += f"- {item}\n"
        else:
            ashare_md = "## A股联动背景\n\n- 无A股参考数据\n"

        content = (
            f"---\n"
            f"type: midday_refresh\n"
            f"date: {refresh_date}\n"
            f"added_count: {added_count}\n"
            f"total_count: {total_count}\n"
            f"tags: [midday, plan, refresh]\n"
            f"---\n\n"
            f"# 午间刷新 ({refresh_date})\n\n"
            f"## 摘要\n\n"
            f"- **新增数量**: {added_count}\n"
            f"- **目标总数**: {total_count}\n\n"
            f"{targets_md}\n"
            f"{ashare_md}\n"
        )
        self.writer._write_md("daily", f"{refresh_date}_midday_refresh.md", content, overwrite=True)

    # ── review.eod ────────────────────────────────────────────────

    def _on_review_eod(self, event: DomainEvent) -> None:
        d = event.data
        trade_date = d.get("trade_date", str(_date.today()))

        self.writer.write_daily_postmarket({
            "date": trade_date,
            "market": d.get("market", "HK"),
            "total_trades": d.get("trades_reviewed", 0),
            "win_rate": d.get("win_rate", 0),
            "total_pnl": d.get("total_pnl", 0),
            "max_drawdown": d.get("max_drawdown", 0),
            "trades": [
                {"code": t["code"], "pnl": t.get("pnl", 0), "id": str(i)}
                for i, t in enumerate(d.get("trades", []))
            ],
        })

        summary = d.get("summary", "")
        if summary:
            self.writer._write_md("daily", f"{trade_date}_eod_full_report.md", summary, overwrite=True)

        for lesson in d.get("lessons", []):
            self.writer.write_lesson(lesson)

    # ── review.nextday ────────────────────────────────────────────

    def _on_review_nextday(self, event: DomainEvent) -> None:
        d = event.data
        trade_date = d.get("trade_date", str(_date.today()))
        preview_text = d.get("preview_text", "")
        if not preview_text:
            return
        self.writer._write_md(
            "daily",
            f"{trade_date}_next_day_preview.md",
            f"---\ntype: next_day_preview\nmarket: HK\ndate: {trade_date}\ntags: [prediction, next-day]\n---\n\n# 次日预判 ({trade_date})\n\n{preview_text}\n",
            overwrite=True,
        )

    # ── review.midday ─────────────────────────────────────────────

    def _on_review_midday(self, event: DomainEvent) -> None:
        d = event.data
        review_date = d.get("date", str(_date.today()))
        result = d.get("result", {})
        lessons_added = d.get("lessons_added", 0)
        status = d.get("status", "completed")
        content = (
            f"---\n"
            f"type: midday_review\n"
            f"date: {review_date}\n"
            f"trades_reviewed: {d.get('trades_reviewed', 0)}\n"
            f"lessons_added: {lessons_added}\n"
            f"status: {status}\n"
            f"tags: [midday, review]\n"
            f"---\n\n"
            f"# 午间复盘 ({review_date})\n\n"
            f"## 复盘结果\n\n"
            f"```json\n{_json.dumps(result, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )
        self.writer._write_md("daily", f"{review_date}_midday_review.md", content, overwrite=True)

    # ── evolution.weekly ──────────────────────────────────────────

    def _on_evolution_weekly(self, event: DomainEvent) -> None:
        d = event.data
        evo_date = d.get("date", str(_date.today()))
        evo = d.get("evolve_result", {})

        self.writer._write_md(
            "attribution",
            f"{evo_date}_knowledge_evolution.md",
            f"---\ntype: knowledge_evolution\ndate: {evo_date}\ntags: [evolution, knowledge, weekly]\n---\n\n"
            f"# 知识进化周报 ({evo_date})\n\n"
            f"## 知识摘要\n```json\n{_json.dumps(evo.get('knowledge_summary', {}), ensure_ascii=False, indent=2)}\n```\n\n"
            f"## 已确认模式\n{_json.dumps(evo.get('confirmed_patterns', []), ensure_ascii=False, indent=2)}\n\n"
            f"## 跨标的洞察\n{_json.dumps(evo.get('cross_symbol_insights', []), ensure_ascii=False, indent=2)}\n",
            overwrite=True,
        )

        report = d.get("meta_report", "")
        meta_score = d.get("meta_score", 0)
        if report:
            self.writer._write_md(
                "strategies",
                f"{evo_date}_meta_analysis.md",
                f"---\ntype: meta_analysis\ndate: {evo_date}\nscore: {meta_score}\ntags: [meta, analysis, weekly]\n---\n\n{report}\n",
                overwrite=True,
            )

    # ── system.risk_adjusted (v2) ─────────────────────────────────

    def _on_system_risk_adjusted(self, event: DomainEvent) -> None:
        """自适应风控调整 → 写入 risk/adjustments/{date}_risk_adjustment.md"""
        d = event.data
        adj_date = str(_date.today())
        reason = d.get("reason", "unknown")
        adjustment = d.get("adjustment", "")
        old_value = d.get("old_value", 0)
        new_value = d.get("new_value", 0)
        consecutive_stops = d.get("consecutive_stops", 0)
        affected_code = d.get("affected_code", "")

        content = (
            f"---\n"
            f"type: risk_adjustment\n"
            f"date: {adj_date}\n"
            f"reason: {reason}\n"
            f"consecutive_stops: {consecutive_stops}\n"
            f"affected_code: {affected_code or 'N/A'}\n"
            f"tags: [risk, adjustment, adaptive]\n"
            f"---\n\n"
            f"# 自适应风控调整 ({adj_date})\n\n"
            f"## 触发原因\n\n{reason}\n\n"
            f"## 调整内容\n\n{adjustment}\n\n"
            f"## 参数变化\n\n"
            f"| 参数 | 旧值 | 新值 |\n"
            f"|------|------|------|\n"
            f"| value | {old_value} | {new_value} |\n\n"
        )
        if affected_code:
            content += f"## 影响标的\n\n- {affected_code}\n\n"
        if consecutive_stops > 0:
            content += f"## 连续止损计数\n\n{consecutive_stops}\n\n"
        content += (
            f"## 关联\n\n"
            f"- 近期的止损笔记请查看 trades/ 目录\n"
            f"- 相关决策: decisions/\n"
        )
        self.writer._write_md("risk/adjustments", f"{adj_date}_risk_adjustment.md", content, overwrite=True)
