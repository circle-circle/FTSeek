"""
港股记忆注入器 (HKMemoryInjector)

职责:
1. 盘前从 hk.json + hk_lessons.db 加载历史教训
2. Token 预算控制 (max 8000 chars)
3. 按优先级注入: 高亏损 > 重复模式 > 一般教训 > 长期记忆
4. 输出 feedback_context 供 PreMarketAnalyst 使用

设计原则:
- 纯读取，零副作用
- hk.json 不存在时降级为"无历史教训"
- 过期教训自动过滤
- Token 预算严格控制
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.review.hk_memory_injector")

MEMORY_FILE = os.path.expanduser("~/.hermes/ftseek/memory/hk.json")
LESSONS_DB = os.environ.get("FTSEEK_HK_LESSONS_DB", "data/hk_lessons.db")

MAX_TOTAL_CHARS = 8000


class HKMemoryInjector:
    """港股记忆注入器 — 盘前教训 → LLM Prompt"""

    def __init__(self, memory_file: str = None, lessons_db: str = None):
        self.memory_file = memory_file or MEMORY_FILE
        self.lessons_db = lessons_db or LESSONS_DB

    def inject(self, context: Dict = None) -> str:
        """构建 feedback_context 注入到 PreMarketAnalyst

        Args:
            context: 当前市场上下文 {sectors, symbols, keywords}，用于相关性评分

        Returns:
            格式化的教训文本 (≤ 8000 chars)
        """
        sections = []
        budget = MAX_TOTAL_CHARS

        # 优先级 0: 昨日盘后次日预判 (最高优先级，直接相关)
        next_day = self._load_next_day_preview()
        if next_day:
            sections.append(next_day)
            budget -= len(next_day)

        # 优先级 0b: 昨日实战表现 (从 feedback relay 加载)
        if budget > 300:
            feedback = self._load_yesterday_feedback_relay()
            if feedback:
                sections.append(feedback)
                budget -= len(feedback)

        # 优先级 1: 上下文感知注入 (使用知识积累器的相关性评分)
        if context and budget > 500:
            try:
                from app.review.knowledge_accumulator import get_knowledge_accumulator
                acc = get_knowledge_accumulator()
                contextual = acc.get_contextual_lessons(context, max_chars=min(budget, 4000))
                if contextual:
                    section = self._format_lessons(contextual, tag="📊 上下文相关教训")
                    sections.append(section)
                    budget -= len(section)
            except Exception as e:
                logger.debug(f"[HKMemoryInjector] 上下文注入失败，回退到传统模式: {e}")

        # 优先级 2: 最近 7 天高亏损教训 (上下文注入的补充)
        high_loss = self._get_high_loss_lessons(days=7, limit=5)
        if high_loss:
            section = self._format_lessons(high_loss, tag="⚠️ 高亏损教训")
            sections.append(section)
            budget -= len(section)

        # 优先级 2: 重复犯错模式
        if budget > 500:
            patterns = self._identify_recurring_patterns(limit=3)
            if patterns:
                section = self._format_patterns(patterns)
                sections.append(section)
                budget -= len(section)

        # 优先级 3: 一般教训 (剩余预算内)
        if budget > 1000:
            general = self._get_general_lessons(days=7, max_chars=budget)
            if general:
                sections.append(self._format_lessons(general, tag="📝 近期教训"))

        # 优先级 4: Agent 长期记忆
        if budget > 500:
            memory = self._load_hk_memory(max_chars=budget)
            if memory:
                sections.append(memory)

        if not sections:
            return "【无历史教训】请基于当前信息独立推理，注意港股 T+0 特性、整手股数要求、A 股联动影响。"

        result = "\n\n".join(sections)
        # 最终截断确保不超过预算
        if len(result) > MAX_TOTAL_CHARS:
            result = result[: MAX_TOTAL_CHARS - 50] + "\n\n...(Token 预算限制，部分教训已截断)"

        logger.info(
            f"[HKMemoryInjector] 记忆注入完成: {len(sections)} 个区块, "
            f"{len(result)} chars / {MAX_TOTAL_CHARS}"
        )
        return result

    # ──────────────────────────────────────────────────────────────
    #  教训查询
    # ──────────────────────────────────────────────────────────────

    def _get_high_loss_lessons(self, days: int = 7, limit: int = 5) -> List[Dict]:
        """获取高亏损教训 (pnl < -1%)"""
        try:
            if not os.path.exists(self.lessons_db):
                return []

            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            conn = sqlite3.connect(self.lessons_db)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT * FROM lessons
                WHERE trade_date >= ?
                  AND pnl_percent < -1.0
                  AND expiry_date >= ?
                  AND validation_status != 'invalidated'
                ORDER BY pnl_percent ASC
                LIMIT ?
                """,
                (cutoff, datetime.now().strftime("%Y-%m-%d"), limit),
            )

            rows = cursor.fetchall()
            conn.close()

            return [dict(r) for r in rows]

        except Exception as e:
            logger.debug(f"[HKMemoryInjector] 高亏损教训查询失败: {e}")
            return []

    def _get_general_lessons(
        self, days: int = 7, max_chars: int = 2000
    ) -> List[Dict]:
        """获取一般教训 (在预算范围内)"""
        try:
            if not os.path.exists(self.lessons_db):
                return []

            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            conn = sqlite3.connect(self.lessons_db)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 排除已选的高亏损教训
            cursor.execute(
                """
                SELECT * FROM lessons
                WHERE trade_date >= ?
                  AND expiry_date >= ?
                  AND validation_status != 'invalidated'
                ORDER BY
                    CASE
                        WHEN pnl_percent < -2 THEN 1
                        WHEN pnl_percent < 0 THEN 2
                        WHEN pnl_percent > 2 THEN 3
                        ELSE 4
                    END,
                    created_at DESC
                """,
                (cutoff, datetime.now().strftime("%Y-%m-%d")),
            )

            rows = cursor.fetchall()
            conn.close()

            # 预算截断
            lessons = []
            total_chars = 0
            for row in rows:
                lesson_text = dict(row).get("lesson", "")
                if total_chars + len(lesson_text) > max_chars:
                    break
                lessons.append(dict(row))
                total_chars += len(lesson_text)

            return lessons

        except Exception as e:
            logger.debug(f"[HKMemoryInjector] 一般教训查询失败: {e}")
            return []

    # ──────────────────────────────────────────────────────────────
    #  模式识别
    # ──────────────────────────────────────────────────────────────

    def _identify_recurring_patterns(self, limit: int = 3) -> List[Dict]:
        """识别重复犯错模式"""
        try:
            if not os.path.exists(self.lessons_db):
                return []

            conn = sqlite3.connect(self.lessons_db)
            cursor = conn.cursor()

            # 同 symbol 重复亏损
            cursor.execute(
                """
                SELECT symbol, COUNT(*) as loss_count,
                       AVG(pnl_percent) as avg_loss,
                       GROUP_CONCAT(DISTINCT cause) as causes
                FROM lessons
                WHERE pnl_percent < -0.5
                  AND expiry_date >= ?
                GROUP BY symbol
                HAVING loss_count >= 2
                ORDER BY avg_loss ASC
                LIMIT ?
                """,
                (datetime.now().strftime("%Y-%m-%d"), limit),
            )

            rows = cursor.fetchall()
            conn.close()

            patterns = []
            for row in rows:
                symbol, count, avg_loss, causes = row
                patterns.append(
                    {
                        "symbol": symbol,
                        "count": count,
                        "avg_loss": round(avg_loss, 2),
                        "causes": causes,
                        "pattern_text": (
                            f"🔴 {symbol} 近 14 天 {count} 次亏损，"
                            f"平均亏损 {avg_loss:.1f}%，原因: {causes}"
                        ),
                    }
                )

            return patterns

        except Exception as e:
            logger.debug(f"[HKMemoryInjector] 模式识别失败: {e}")
            return []

    # ──────────────────────────────────────────────────────────────
    #  Agent 记忆 (hk.json)
    # ──────────────────────────────────────────────────────────────

    def _load_hk_memory(self, max_chars: int = 2000) -> Optional[str]:
        """加载 hk.json Agent 记忆"""
        try:
            if not os.path.exists(self.memory_file):
                return None

            with open(self.memory_file, "r", encoding="utf-8") as f:
                memory = json.load(f)

            parts = []

            # 核心原则
            if memory.get("core_principle"):
                parts.append(f"【核心原则】{memory['core_principle']}")

            # 市场规则
            rules = memory.get("market_specific_rules", [])
            if rules:
                rules_text = "\n".join(f"- {r}" for r in rules[:5])
                parts.append(f"【市场规则】\n{rules_text}")

            # 历史教训 (从记忆文件中提取)
            lessons = memory.get("historical_lessons", [])
            if lessons:
                lesson_text = "\n".join(f"- {l}" for l in lessons[:5])
                parts.append(f"【历史教训】\n{lesson_text}")

            # 个股教训
            symbol_lessons = memory.get("symbol_lessons", {})
            if symbol_lessons:
                sl_parts = []
                for sym, lesson in list(symbol_lessons.items())[:3]:
                    sl_parts.append(f"- {sym}: {lesson}")
                parts.append(f"【个股经验】\n" + "\n".join(sl_parts))

            if not parts:
                return None

            result = "【Agent 长期记忆】\n" + "\n\n".join(parts)

            # 预算截断
            if len(result) > max_chars:
                result = result[:max_chars] + "\n...(记忆截断)"

            return result

        except Exception as e:
            logger.debug(f"[HKMemoryInjector] 加载 hk.json 失败: {e}")
            return None

    # ──────────────────────────────────────────────────────────────
    #  格式化
    # ──────────────────────────────────────────────────────────────

    def _format_lessons(self, lessons: List[Dict], tag: str = "") -> str:
        """格式化教训列表"""
        lines = [f"【{tag}】"] if tag else []

        for lesson in lessons:
            symbol = lesson.get("symbol", "?")
            pnl = lesson.get("pnl_percent", 0)
            cause = lesson.get("cause", "")
            text = lesson.get("lesson", "")

            emoji = "🔴" if pnl < -2 else "🟡" if pnl < 0 else "🟢"
            lines.append(f"{emoji} {symbol} ({pnl:+.1f}%) [{cause}]")
            lines.append(f"   {text}")

        return "\n".join(lines)

    def _format_patterns(self, patterns: List[Dict]) -> str:
        """格式化重复模式"""
        lines = ["【⚠️ 重复犯错模式】"]
        for p in patterns:
            lines.append(p["pattern_text"])
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    #  工具方法
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_memory_file_exists():
        """初始化 hk.json (仅首次)"""
        if os.path.exists(MEMORY_FILE):
            return

        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)

        default = {
            "market": "港股",
            "core_principle": "稳定赚钱 > 激进收益；宁可错过，不要做错",
            "market_specific_rules": [
                "港股 T+0 可日内买卖，但追高风险极高",
                "港股受 A 股和美股双重影响，注意联动",
                "整手股数因股而异，下单前必须对齐整手倍数",
                "午间休市 (12:00-13:00) 期间 A 股午盘走势影响港股下午开盘",
                "南向资金流向是重要参考指标",
                "AH 溢价 > 140% 时 A-H 联动可能失效",
            ],
            "historical_lessons": [],
            "strategy_adjustments": [],
            "symbol_lessons": {},
            "next_day_preview": "",
            "performance_tracking": {
                "total_trades": 0,
                "win_rate": 0.0,
                "max_drawdown": 0.0,
            },
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)

        logger.info(f"[HKMemoryInjector] hk.json 已初始化: {MEMORY_FILE}")

    def inject_next_day_preview(self, text: str):
        """保存次日预判到 hk.json，供明天盘前使用"""
        try:
            if not os.path.exists(self.memory_file):
                self.ensure_memory_file_exists()

            with open(self.memory_file, "r", encoding="utf-8") as f:
                memory = json.load(f)

            memory["next_day_preview"] = text
            memory["updated_at"] = datetime.now().isoformat()

            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=2, ensure_ascii=False)

            logger.info(f"[HKMemoryInjector] 次日预判已保存 ({len(text)} chars)")
        except Exception as e:
            logger.error(f"[HKMemoryInjector] 保存次日预判失败: {e}")

    def _load_next_day_preview(self) -> Optional[str]:
        """加载昨日盘后的次日预判"""
        try:
            if not os.path.exists(self.memory_file):
                return None

            with open(self.memory_file, "r", encoding="utf-8") as f:
                memory = json.load(f)

            return memory.get("next_day_preview", "") or None
        except Exception as e:
            logger.debug(f"[HKMemoryInjector] 加载次日预判失败: {e}")
            return None

    def _load_yesterday_feedback_relay(self) -> Optional[str]:
        """从 feedback relay 加载昨日 EOD 反馈 (实战表现 + 教训 + 计划执行)"""
        try:
            from app.review.hk_feedback_relay import load_yesterday_feedback
            fb = load_yesterday_feedback()
            if not fb:
                return None

            parts = ["📊 昨日实战表现 (来自 EOD 反馈)"]
            perf = fb.get("performance", {})
            if perf.get("total_trades"):
                parts.append(
                    f"  交易: {perf['total_trades']}笔 | "
                    f"胜率: {perf['win_count']}/{perf['total_trades']} | "
                    f"总盈亏: {perf['total_pnl']:+.0f}"
                )

            # 次日预判
            if fb.get("next_day_preview"):
                parts.append(f"\n  【昨日预判今日需验证】\n  {fb['next_day_preview'][:500]}")

            # 作战计划执行
            bp = fb.get("battle_plan_execution", {})
            if bp:
                parts.append(f"\n  【昨日计划执行】\n  {str(bp)[:300]}")

            # 最新教训 (最多 3 条)
            lessons = fb.get("lessons", [])[:3]
            if lessons:
                parts.append("\n  【昨日教训】")
                for l in lessons:
                    title = l.get("title", l.get("lesson", ""))[:80]
                    parts.append(f"  • {title}")

            return "\n".join(parts)
        except Exception as e:
            logger.debug(f"[HKMemoryInjector] 加载 feedback relay 失败: {e}")
            return None

    def update_memory(self, lessons: List[Dict]):
        """更新 hk.json (追加新教训)"""
        try:
            if not os.path.exists(self.memory_file):
                self.ensure_memory_file_exists()

            with open(self.memory_file, "r", encoding="utf-8") as f:
                memory = json.load(f)

            # 追加教训
            existing = memory.get("historical_lessons", [])
            for lesson in lessons:
                lesson_text = lesson.get("lesson", "")
                if lesson_text and lesson_text not in existing:
                    existing.append(lesson_text)

            # 保留最近 20 条
            memory["historical_lessons"] = existing[-20:]

            # 更新个股教训
            symbol_lessons = memory.get("symbol_lessons", {})
            for lesson in lessons:
                symbol = lesson.get("symbol", "")
                if symbol and lesson.get("pnl_percent", 0) < -2:
                    symbol_lessons[symbol] = lesson.get("lesson", "")

            memory["symbol_lessons"] = symbol_lessons
            memory["updated_at"] = datetime.now().isoformat()

            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=2, ensure_ascii=False)

            logger.info(
                f"[HKMemoryInjector] hk.json 已更新: {len(lessons)} 条教训"
            )

        except Exception as e:
            logger.error(f"[HKMemoryInjector] 更新 hk.json 失败: {e}")


# 模块级单例
_instance = None


def get_hk_memory_injector() -> HKMemoryInjector:
    """获取单例 HKMemoryInjector"""
    global _instance
    if _instance is None:
        _instance = HKMemoryInjector()
    return _instance
