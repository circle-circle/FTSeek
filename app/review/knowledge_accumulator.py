"""
知识积累器 (Knowledge Accumulator)

驱动系统自我进化:
1. 模式检测: 按 (cause, sector) 分组，识别重复出现的错误模式
2. 置信度进化: 替代简单过期模型，基于重现次数和验证结果动态调整
3. 智能注入: 基于上下文的 TF-IDF 式相关性评分，替代简单按时间排序

核心思想: 教训不是"过期的"，而是"验证的"或"推翻的"。
置信度随重现而增长，随时间衰减，随新证据而推翻。
"""

import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("ftseek.review.knowledge_accumulator")

LESSONS_DB = os.environ.get("FTSEEK_HK_LESSONS_DB", "data/hk_lessons.db")


class KnowledgeAccumulator:
    """知识积累器 — 模式检测 + 置信度进化 + 智能注入"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or LESSONS_DB
        self._ensure_schema()

    def _ensure_schema(self):
        """确保 DB schema 包含知识积累所需字段"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 检查并添加新列
            existing = {row[1] for row in cursor.execute("PRAGMA table_info(lessons)").fetchall()}

            migrations = [
                ("pattern_group_id", "TEXT DEFAULT ''"),
                ("confidence_score", "REAL DEFAULT 0.5"),
                ("occurrence_count", "INTEGER DEFAULT 1"),
                ("last_seen_date", "TEXT DEFAULT ''"),
                ("validation_reason", "TEXT DEFAULT ''"),
                ("sector", "TEXT DEFAULT ''"),
            ]

            for col_name, col_def in migrations:
                if col_name not in existing:
                    cursor.execute(f"ALTER TABLE lessons ADD COLUMN {col_name} {col_def}")
                    logger.info(f"[KnowledgeAccumulator] 新增列: {col_name}")

            # 新索引
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_lessons_pattern ON lessons(pattern_group_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_lessons_confidence ON lessons(confidence_score)"
            )

            conn.commit()
            conn.close()
            logger.info("[KnowledgeAccumulator] Schema 已确保最新")
        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] Schema 迁移失败: {e}")

    # ════════════════════════════════════════════════════════════════
    #  模式检测 (每周末运行)
    # ════════════════════════════════════════════════════════════════

    def detect_patterns(self, min_occurrences: int = 3,
                         min_weeks_span: int = 2) -> List[Dict]:
        """检测确认的行为模式

        Returns:
            已确认的模式列表，每个包含:
            - pattern_id, cause, sector, symbols, count, avg_loss, weeks_span
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查找相同 cause + sector 组合出现 ≥3 次的情况
            cursor.execute(
                """
                SELECT
                    COALESCE(NULLIF(cause, ''), 'unknown') as cause_group,
                    COALESCE(NULLIF(sector, ''), 'general') as sector_group,
                    GROUP_CONCAT(DISTINCT symbol) as symbols,
                    COUNT(*) as cnt,
                    AVG(pnl_percent) as avg_loss,
                    MIN(trade_date) as first_seen,
                    MAX(trade_date) as last_seen
                FROM lessons
                WHERE pnl_percent < 0
                  AND validation_status != 'invalidated'
                GROUP BY cause_group, sector_group
                HAVING cnt >= ?
                ORDER BY avg_loss ASC
                """,
                (min_occurrences,),
            )

            patterns = []
            for row in cursor.fetchall():
                d = dict(row)
                first = datetime.strptime(d["first_seen"], "%Y-%m-%d")
                last = datetime.strptime(d["last_seen"], "%Y-%m-%d")
                weeks_span = max(1, (last - first).days // 7)

                if weeks_span < min_weeks_span:
                    continue

                pattern_id = f"pattern_{d['cause_group']}_{d['sector_group']}"
                patterns.append({
                    "pattern_id": pattern_id,
                    "cause": d["cause_group"],
                    "sector": d["sector_group"],
                    "symbols": d["symbols"],
                    "occurrence_count": d["cnt"],
                    "avg_loss_pct": round(d["avg_loss"], 2),
                    "weeks_span": weeks_span,
                    "first_seen": d["first_seen"],
                    "last_seen": d["last_seen"],
                    "is_confirmed": weeks_span >= min_weeks_span and d["cnt"] >= min_occurrences,
                })

            conn.close()

            # 标记已确认模式到 DB
            for p in patterns:
                self._tag_pattern_group(p["pattern_id"], p["cause"], p["sector"])

            logger.info(
                f"[KnowledgeAccumulator] 模式检测完成: {len(patterns)} 个模式 "
                f"(确认={sum(1 for p in patterns if p['is_confirmed'])})"
            )
            return patterns

        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] 模式检测失败: {e}")
            return []

    def _tag_pattern_group(self, pattern_id: str, cause: str, sector: str):
        """将匹配的教训标记到同一模式组"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                UPDATE lessons
                SET pattern_group_id = ?
                WHERE cause = ? AND (sector = ? OR (sector = '' AND ? = 'general'))
                  AND pattern_group_id = ''
                """,
                (pattern_id, cause, sector, sector),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[KnowledgeAccumulator] 标记模式组失败: {e}")

    # ════════════════════════════════════════════════════════════════
    #  置信度进化 (替代过期模型)
    # ════════════════════════════════════════════════════════════════

    def evolve_confidence(self):
        """更新所有教训的置信度评分

        规则:
        - 初始: 0.5
        - 每次相似模式重现: +0.1 (上限 0.95)
        - 长期未重现 (>14天): -0.05/周 (下限 0.1)
        - 被验证为正确的教训: +0.2 (上限 0.95)
        - 被推翻的教训: validation_status='invalidated', confidence→0
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            today = datetime.now().strftime("%Y-%m-%d")

            # 1. 基于 pattern_group_id 统计出现次数
            cursor = conn.execute(
                """
                SELECT pattern_group_id, COUNT(*) as cnt
                FROM lessons
                WHERE pattern_group_id != ''
                  AND validation_status != 'invalidated'
                GROUP BY pattern_group_id
                """
            )
            pattern_counts = {row["pattern_group_id"]: row["cnt"] for row in cursor.fetchall()}

            # 2. 逐条更新
            cursor = conn.execute(
                """
                SELECT id, pattern_group_id, confidence_score, occurrence_count,
                       last_seen_date, trade_date, pnl_percent, validation_status
                FROM lessons
                WHERE validation_status != 'invalidated'
                """
            )

            updates = []
            for row in cursor.fetchall():
                d = dict(row)
                confidence = d["confidence_score"] or 0.5
                occ_count = d["occurrence_count"] or 1
                last_seen = d["last_seen_date"] or d["trade_date"]

                # 模式组内出现次数
                if d["pattern_group_id"] and d["pattern_group_id"] in pattern_counts:
                    group_cnt = pattern_counts[d["pattern_group_id"]]
                    if group_cnt > occ_count:
                        confidence = min(0.95, confidence + 0.1 * (group_cnt - occ_count))
                        occ_count = group_cnt

                # 时间衰减 (14天未重现)
                if last_seen:
                    try:
                        last_dt = datetime.strptime(last_seen, "%Y-%m-%d")
                        days_since = (datetime.now() - last_dt).days
                        if days_since > 14:
                            weeks = (days_since - 14) // 7
                            confidence = max(0.1, confidence - 0.05 * weeks)
                    except ValueError:
                        pass

                # 盈利交易经验: 方向正确 → 提升信心
                if d["pnl_percent"] and d["pnl_percent"] > 1:
                    confidence = min(0.95, confidence + 0.05)

                # 更新 last_seen_date
                updates.append((
                    round(confidence, 3), occ_count, today,
                    d["id"],
                ))

            conn.executemany(
                """
                UPDATE lessons
                SET confidence_score = ?, occurrence_count = ?, last_seen_date = ?
                WHERE id = ?
                """,
                updates,
            )

            conn.commit()
            conn.close()

            logger.info(
                f"[KnowledgeAccumulator] 置信度进化完成: {len(updates)} 条教训已更新"
            )

        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] 置信度进化失败: {e}")

    # ════════════════════════════════════════════════════════════════
    #  教训验证
    # ════════════════════════════════════════════════════════════════

    def validate_lesson(self, lesson_id: str, is_valid: bool, reason: str = ""):
        """验证或推翻一条教训"""
        try:
            conn = sqlite3.connect(self.db_path)
            if is_valid:
                conn.execute(
                    """
                    UPDATE lessons
                    SET validation_status = 'validated',
                        confidence_score = MIN(0.95, confidence_score + 0.2),
                        validation_reason = ?
                    WHERE id = ?
                    """,
                    (reason, lesson_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE lessons
                    SET validation_status = 'invalidated',
                        confidence_score = 0.0,
                        validation_reason = ?
                    WHERE id = ?
                    """,
                    (reason, lesson_id),
                )
            conn.commit()
            conn.close()
            logger.info(f"[KnowledgeAccumulator] 教训 {lesson_id}: {'验证' if is_valid else '推翻'} ({reason})")
        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] 验证教训失败: {e}")

    # ════════════════════════════════════════════════════════════════
    #  上下文感知注入 (替代简单按时间/亏损排序)
    # ════════════════════════════════════════════════════════════════

    def get_contextual_lessons(self, context: Dict, max_chars: int = 4000,
                                min_confidence: float = 0.3) -> List[Dict]:
        """基于上下文相关性获取最相关的教训

        Args:
            context: 当前市场上下文 (包含 sectors, symbols, keywords 等)
            max_chars: 最大字符数预算
            min_confidence: 最低置信度阈值

        Returns:
            按相关性排序的教训列表
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # 获取所有活跃教训
            cursor = conn.execute(
                """
                SELECT * FROM lessons
                WHERE validation_status != 'invalidated'
                  AND confidence_score >= ?
                ORDER BY confidence_score DESC, pnl_percent ASC
                """,
                (min_confidence,),
            )

            all_lessons = [dict(r) for r in cursor.fetchall()]
            conn.close()

            if not all_lessons:
                return []

            # 提取上下文特征
            context_sectors = set(context.get("sectors", []))
            context_symbols = set(context.get("symbols", []))
            context_keywords = set(k.lower() for k in context.get("keywords", []))

            # 相关性评分
            scored = []
            for lesson in all_lessons:
                score = lesson.get("confidence_score", 0.5)

                # 行业匹配: +0.3
                lesson_sector = lesson.get("sector", "")
                if lesson_sector and lesson_sector in context_sectors:
                    score += 0.3

                # 标的匹配: +0.4
                lesson_symbol = lesson.get("symbol", "")
                if lesson_symbol in context_symbols:
                    score += 0.4

                # 关键词匹配: +0.1 per keyword
                lesson_text = (lesson.get("lesson", "") + " " +
                              lesson.get("cause", "")).lower()
                for kw in context_keywords:
                    if kw and kw in lesson_text:
                        score += 0.1

                # 高亏损教训加权: +0.2
                if lesson.get("pnl_percent", 0) < -2:
                    score += 0.2

                # 已确认模式加权: +0.3
                if lesson.get("pattern_group_id"):
                    score += 0.3

                scored.append((score, lesson))

            # 按相关性排序
            scored.sort(key=lambda x: x[0], reverse=True)

            # 预算截断
            result = []
            total_chars = 0
            for score, lesson in scored:
                text_len = len(lesson.get("lesson", "")) + 20
                if total_chars + text_len > max_chars:
                    break
                result.append(lesson)
                total_chars += text_len

            logger.info(
                f"[KnowledgeAccumulator] 上下文注入: {len(result)}/{len(all_lessons)} 条 "
                f"({total_chars}/{max_chars} chars)"
            )
            return result

        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] 上下文注入失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════════
    #  跨标的共性分析
    # ════════════════════════════════════════════════════════════════

    def analyze_cross_symbol_patterns(self) -> List[Dict]:
        """检测跨标的共性错误 (如"所有科技股亏损都因追高")"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            cursor = conn.execute(
                """
                SELECT
                    cause,
                    COUNT(DISTINCT symbol) as symbol_count,
                    GROUP_CONCAT(DISTINCT symbol) as symbols,
                    AVG(pnl_percent) as avg_loss,
                    COUNT(*) as total_trades
                FROM lessons
                WHERE pnl_percent < 0
                  AND validation_status != 'invalidated'
                GROUP BY cause
                HAVING symbol_count >= 2
                ORDER BY symbol_count DESC, avg_loss ASC
                """
            )

            patterns = []
            for row in cursor.fetchall():
                d = dict(row)
                patterns.append({
                    "cause": d["cause"],
                    "affected_symbols": d["symbol_count"],
                    "symbols": d["symbols"],
                    "avg_loss": round(d["avg_loss"], 2),
                    "total_trades": d["total_trades"],
                    "insight": (
                        f"跨 {d['symbol_count']} 只标的共性问题 [{d['cause']}]: "
                        f"{d['total_trades']} 次亏损, 平均 {d['avg_loss']:.1f}%"
                    ),
                })

            conn.close()
            return patterns

        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] 跨标的分析失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════════
    #  知识摘要 (供外部使用)
    # ════════════════════════════════════════════════════════════════

    def get_knowledge_summary(self) -> Dict:
        """获取知识库摘要"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            total = conn.execute("SELECT COUNT(*) as c FROM lessons").fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) as c FROM lessons WHERE validation_status != 'invalidated'"
            ).fetchone()["c"]
            invalidated = conn.execute(
                "SELECT COUNT(*) as c FROM lessons WHERE validation_status = 'invalidated'"
            ).fetchone()["c"]
            patterns = conn.execute(
                "SELECT COUNT(DISTINCT pattern_group_id) as c FROM lessons WHERE pattern_group_id != ''"
            ).fetchone()["c"]

            avg_confidence = conn.execute(
                "SELECT AVG(confidence_score) as c FROM lessons WHERE validation_status != 'invalidated'"
            ).fetchone()["c"] or 0

            conn.close()

            return {
                "total_lessons": total,
                "active_lessons": active,
                "invalidated_lessons": invalidated,
                "confirmed_patterns": patterns,
                "avg_confidence": round(avg_confidence, 3),
                "knowledge_maturity": (
                    "established" if patterns >= 3 and avg_confidence > 0.6
                    else "growing" if patterns >= 1
                    else "early"
                ),
            }
        except Exception as e:
            logger.error(f"[KnowledgeAccumulator] 知识摘要失败: {e}")
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  每周进化 (一站式调用)
    # ════════════════════════════════════════════════════════════════

    def weekly_evolve(self) -> Dict:
        """每周知识进化: 检测模式 + 更新置信度 + 跨标的分析 + 生成摘要

        建议每周末运行。
        """
        logger.info("[KnowledgeAccumulator] === 每周知识进化开始 ===")

        patterns = self.detect_patterns()
        self.evolve_confidence()
        cross = self.analyze_cross_symbol_patterns()
        summary = self.get_knowledge_summary()

        result = {
            "patterns_detected": len(patterns),
            "confirmed_patterns": [p for p in patterns if p.get("is_confirmed")],
            "cross_symbol_insights": cross,
            "knowledge_summary": summary,
            "evolved_at": datetime.now().isoformat(),
        }

        logger.info(
            f"[KnowledgeAccumulator] === 每周知识进化完成: "
            f"{len(patterns)} 模式, {len(cross)} 跨标的洞察 ==="
        )
        return result


# 模块级单例
_accumulator: Optional[KnowledgeAccumulator] = None


def get_knowledge_accumulator() -> KnowledgeAccumulator:
    global _accumulator
    if _accumulator is None:
        _accumulator = KnowledgeAccumulator()
    return _accumulator
