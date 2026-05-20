"""
港股午间轻量归因 (MiddayMiniReview)

职责:
1. 12:00-12:30 HKT 触发 (午间休市)
2. 仅处理上午交易 (9:30-12:00)
3. 规则归因 + LLM 深度归因 (亏损>1%触发)
4. 更新 hk.json (追加教训)
5. 推送异常交易摘要到钉钉
6. 正常交易不推送 (减少噪音)

设计原则:
- 极简: < 400 行
- 快速: 规则归因 <2s, LLM 归因异步非阻塞
- 无副作用: 不修改交易状态
- 推送: 仅当有异常交易时推送到钉钉
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.review.midday_mini_review")

FTSEEK_DB = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")
MEMORY_FILE = os.path.expanduser("~/.hermes/ftseek/memory/hk.json")


class MiddayMiniReview:
    """午间轻量归因 — 12:00-12:30 HKT"""

    def run(self, trade_date: Optional[str] = None) -> Dict:
        """执行午间归因

        Returns:
            归因统计摘要
        """
        logger.info("[MiddayMiniReview] === 午间轻量归因启动 ===")

        # 1. 获取上午交易 (9:30-12:00)
        morning_trades = self._get_morning_trades(trade_date)
        if not morning_trades:
            logger.info("[MiddayMiniReview] 上午无交易，跳过")
            return {"trades_reviewed": 0, "lessons_added": 0, "status": "no_trades"}

        # 2. 规则归因
        results = []
        new_lessons = []

        for trade in morning_trades:
            result = self._diagnose(trade)
            results.append(result)

            if result["memory_update"]:
                new_lessons.append(
                    {
                        "id": f"hk_midday_{trade.get('code', '')}_{datetime.now().strftime('%H%M')}",
                        "trade_date": datetime.now().strftime("%Y-%m-%d"),
                        "symbol": trade.get("code", ""),
                        "direction": trade.get("action", "UNKNOWN"),
                        "pnl_percent": trade.get("pnl_percent", 0) or 0,
                        "cause": result["cause"],
                        "lesson": result["lesson"],
                        "confidence": 0.8,
                        "expiry_date": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
                        "validation_status": "pending",
                    }
                )

        # 2.5 LLM 归因 (非阻塞, 仅亏损>1%触发)
        for trade in morning_trades:
            pnl_pct = trade.get("pnl_percent", 0) or 0
            if pnl_pct < -1:
                llm_result = self._run_llm_attribution(trade)
                if llm_result:
                    agent_results = llm_result.get("agent_results", {})
                    dir_results = agent_results.get("direction", [])
                    exec_results = agent_results.get("execution", [])

                    dir_data = dir_results[-1].get("data", {}) if dir_results else {}
                    exec_data = exec_results[-1].get("data", {}) if exec_results else {}

                    lesson_parts = []
                    if dir_data.get("root_cause"):
                        lesson_parts.append(f"[方向] {dir_data['root_cause']}")
                    if dir_data.get("improvement_action"):
                        lesson_parts.append(f"改进: {dir_data['improvement_action']}")
                    if exec_data.get("root_cause") and exec_data.get("cause_category") != "execution_aligned":
                        lesson_parts.append(f"[执行] {exec_data['root_cause']}")
                    if exec_data.get("improvement_action"):
                        lesson_parts.append(f"改进: {exec_data['improvement_action']}")

                    if lesson_parts:
                        new_lessons.append({
                            "id": f"hk_midday_llm_{trade.get('code', '')}_{datetime.now().strftime('%H%M')}",
                            "trade_date": datetime.now().strftime("%Y-%m-%d"),
                            "symbol": trade.get("code", ""),
                            "direction": trade.get("action", "UNKNOWN"),
                            "pnl_percent": pnl_pct,
                            "cause": "llm_attribution",
                            "lesson": " | ".join(lesson_parts),
                            "confidence": 0.7,
                            "expiry_date": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
                            "validation_status": "pending",
                            "llm_attribution": {
                                "direction": dir_data,
                                "execution": exec_data,
                            },
                        })
                        logger.info(
                            f"[MiddayMiniReview] LLM 归因完成: {trade.get('code', '')} "
                            f"方向={dir_data.get('cause_category', '?')} "
                            f"执行={exec_data.get('cause_category', '?')}"
                        )

        # 3. 更新 hk.json (追加教训)
        if new_lessons:
            self._append_to_memory(new_lessons)

        # 4. 写入 hk_lessons.db
        if new_lessons:
            self._save_to_db(new_lessons)

        logger.info(
            f"[MiddayMiniReview] === 完成: {len(morning_trades)} 笔交易, "
            f"{len(new_lessons)} 条教训 ==="
        )

        # 领域事件 — 午间复盘 (Obsidian 知识库)
        from app.core.domain_events import get_event_bus, DomainEvent, EventType, ReviewMiddayPayload
        get_event_bus().emit(DomainEvent(
            type=EventType.REVIEW_MIDDAY,
            data=ReviewMiddayPayload(
                date=trade_date or datetime.now().strftime("%Y-%m-%d"),
                trades_reviewed=len(morning_trades),
                lessons_added=len(new_lessons),
                status='completed',
            )
        ))

        # 5. 推送钉钉通知 (仅异常交易)
        self._maybe_send_notification(morning_trades, results, new_lessons)

        return {
            "trades_reviewed": len(morning_trades),
            "lessons_added": len(new_lessons),
            "results": results,
            "status": "completed",
        }

    # ──────────────────────────────────────────────────────────────
    #  内部方法
    # ──────────────────────────────────────────────────────────────

    def _get_morning_trades(self, trade_date: Optional[str] = None) -> List[Dict]:
        """获取上午交易 (9:30-12:00)"""
        try:
            today = trade_date or datetime.now().strftime("%Y-%m-%d")
            with sqlite3.connect(FTSEEK_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute(
                    """
                    SELECT * FROM trades
                    WHERE code LIKE 'HK.%'
                      AND entry_time >= ? AND entry_time < ?
                    ORDER BY entry_time ASC
                    """,
                    (f"{today} 09:30:00", f"{today} 12:00:00"),
                )

                rows = cursor.fetchall()

            trades = [dict(r) for r in rows]
            logger.debug(f"[MiddayMiniReview] 上午交易: {len(trades)} 笔")
            return trades

        except Exception as e:
            logger.warning(f"[MiddayMiniReview] 读取上午交易失败: {e}")
            return []

    def _diagnose(self, trade: Dict) -> Dict:
        """快速诊断 (规则-based, <10ms)"""
        pnl_pct = trade.get("pnl_percent", 0) or 0
        code = trade.get("code", "?")
        action = trade.get("action", "UNKNOWN")

        cause = ""
        lesson = ""
        should_update = False

        if pnl_pct < -2:
            cause = "morning_large_loss"
            lesson = f"上午 {code} 亏损 {pnl_pct:.1f}%，待盘后详细归因"
            should_update = True

        elif pnl_pct < -1:
            cause = "morning_loss"
            lesson = f"上午 {code} 亏损 {pnl_pct:.1f}%，观察午盘是否反弹"
            should_update = True

        elif pnl_pct > 2:
            cause = "morning_win"
            lesson = f"上午 {code} 盈利 {pnl_pct:.1f}%，策略有效"
            should_update = False  # 盈利教训优先级低

        return {
            "symbol": code,
            "pnl_pct": pnl_pct,
            "cause": cause,
            "lesson": lesson,
            "quick_diagnosis": lesson if lesson else "正常",
            "memory_update": should_update,
        }

    # ─── LLM 归因 (亏损>1%触发) ─────────────────────────────────

    def _build_trade_attribution_context(self, trade: Dict) -> str:
        """构建单笔交易归因上下文 (供 LLM 分析)"""
        code = trade.get("code", "?")
        action = trade.get("action", "UNKNOWN")
        pnl = trade.get("pnl", 0) or 0
        pnl_pct = trade.get("pnl_percent", 0) or 0
        entry_px = trade.get("entry_price", 0) or 0
        exit_px = trade.get("exit_price", 0) or 0
        qty = trade.get("quantity", 0) or 0
        entry_time = trade.get("entry_time", "")
        exit_time = trade.get("exit_time", "")
        exit_reason = trade.get("exit_reason", "")

        sig = trade.get("signal_summary", {}) or {}
        decision_reason = sig.get("decision_reason", "")
        market_regime = sig.get("market_regime", "")
        ai_sentiment = sig.get("ai_sentiment", 0)

        lines = [
            f"标的: {code}",
            f"方向: {action}",
            f"数量: {qty}",
            f"入场价: {entry_px}",
            f"出场价: {exit_px}",
            f"入场时间: {entry_time}",
            f"出场时间: {exit_time}",
            f"盈亏: {pnl:+.2f} ({pnl_pct:+.2%})",
            f"出场原因: {exit_reason or '无'}",
            f"决策依据: {decision_reason or '无'}",
            f"市场状态: {market_regime or '无'}",
            f"AI情绪评分: {ai_sentiment}",
        ]
        return "\n".join(lines)

    def _run_llm_attribution(self, trade: Dict) -> Optional[Dict]:
        """对单笔亏损交易执行 LLM 归因 (非阻塞, 失败返回 None)"""
        try:
            from app.agents.registry import get_registry, AnalysisScenario
            ctx = self._build_trade_attribution_context(trade)
            registry = get_registry()
            result = registry.run_sync(
                AnalysisScenario.ATTRIBUTION,
                {"default": ctx},
                timeout=90,
            )
            return result
        except Exception as e:
            logger.warning(f"[MiddayMiniReview] LLM 归因失败 (降级为规则): {e}")
            return None

    def _maybe_send_notification(self, trades: List[Dict], results: List[Dict],
                                  new_lessons: List[Dict]) -> None:
        """仅当有异常交易时才推送午间摘要到钉钉"""
        anomalies = [r for r in results if r["cause"] in ("morning_large_loss", "morning_loss")]
        if not anomalies:
            logger.info("[MiddayMiniReview] 上午无异常交易，跳过钉钉推送")
            return

        try:
            from app.notifier.unified import get_unified_notifier
            notifier = get_unified_notifier()
            if not notifier:
                logger.warning("[MiddayMiniReview] notifier 不可用，跳过推送")
                return

            wins = sum(1 for t in trades if (t.get("pnl_percent") or 0) > 0)
            losses = sum(1 for t in trades if (t.get("pnl_percent") or 0) < 0)
            total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)

            lines = [
                "## 午间交易摘要 (港股上午)",
                f"交易笔数: {len(trades)} | 盈利: {wins} | 亏损: {losses} | 净盈亏: {total_pnl:+.2f}",
                "",
                "异常关注:",
            ]
            for r in anomalies:
                emoji = "🔴" if r["pnl_pct"] < -2 else "🟡"
                lines.append(f"  {emoji} {r['symbol']} {r['pnl_pct']:+.1f}% — {r['lesson']}")

            if new_lessons:
                lines.append("")
                lines.append(f"已录入 {len(new_lessons)} 条教训")

            notifier.send_text("\n".join(lines))
            logger.info(f"[MiddayMiniReview] 午间摘要已推送到钉钉 ({len(anomalies)} 项异常)")

        except Exception as e:
            logger.warning(f"[MiddayMiniReview] 钉钉推送失败: {e}")

    def _append_to_memory(self, lessons: List[Dict]):
        """追加到 hk.json"""
        try:
            if not os.path.exists(MEMORY_FILE):
                from app.review.hk_memory_injector import HKMemoryInjector
                HKMemoryInjector.ensure_memory_file_exists()

            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memory = json.load(f)

            existing = memory.get("historical_lessons", [])
            for lesson in lessons:
                text = lesson.get("lesson", "")
                if text and text not in existing:
                    existing.append(text)

            memory["historical_lessons"] = existing[-20:]  # 保留最近 20 条
            memory["updated_at"] = datetime.now().isoformat()

            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=2, ensure_ascii=False)

            logger.info(f"[MiddayMiniReview] hk.json 已更新: {len(lessons)} 条教训")

        except Exception as e:
            logger.error(f"[MiddayMiniReview] 更新 hk.json 失败: {e}")

    def _save_to_db(self, lessons: List[Dict]):
        """写入 hk_lessons.db"""
        try:
            db_path = os.environ.get("FTSEEK_HK_LESSONS_DB", "data/hk_lessons.db")
            with sqlite3.connect(db_path) as conn:
                for lesson in lessons:
                    context_json = json.dumps({})
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO lessons
                        (id, trade_date, symbol, direction, pnl_percent, cause, lesson,
                         confidence, expiry_date, market_context)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lesson["id"],
                            lesson["trade_date"],
                            lesson["symbol"],
                            lesson["direction"],
                            lesson["pnl_percent"],
                            lesson["cause"],
                            lesson["lesson"],
                            lesson.get("confidence", 0.8),
                            lesson["expiry_date"],
                            context_json,
                        ),
                    )
                conn.commit()
        except Exception as e:
            logger.error(f"[MiddayMiniReview] 写入 DB 失败: {e}")
