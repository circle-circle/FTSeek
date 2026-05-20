"""
长期记忆注入器 — 为辩论 Agent 注入完整上下文

职责:
1. 整合 Lessons DB (活跃教训)
2. 加载 us.json (美股独立记忆)
3. 查询历史交易数据
4. 提取模式 (反复出现的错误/成功策略)
5. 格式化为辩论 Prompt 可用的文本

设计原则:
- 纯读取，零副作用
- 低耦合: 不依赖辩论流程，可独立复用
- 缓存: 避免每次辩论都重新查询 DB
"""
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek_us.debate.memory_injector")

# P2 修复 (2026-05-03): 路径通过环境变量可配置
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = os.getenv("FTSEEK_DB_PATH", str(PROJECT_ROOT / "data" / "ftseek_us.db"))
MEMORY_FILE = os.getenv("FTSEEK_MEMORY_FILE", str(Path.home() / ".hermes" / "ftseek" / "memory" / "us.json"))


class MemoryInjector:
    """辩论记忆注入器 — 整合所有长期记忆源"""

    def __init__(self, db_path: str = None, memory_file: str = None, cache_ttl: int = 300):
        self.db_path = db_path or DB_PATH
        self.memory_file = memory_file or MEMORY_FILE
        self.cache_ttl = cache_ttl
        self._cache = None
        self._cache_time = 0

    def inject(self) -> Dict:
        """
        注入所有记忆，返回结构化的上下文字典

        Returns:
            {
                "lessons_text": str,       # 格式化教训文本
                "lessons_count": int,
                "memory_context": str,     # us.json 记忆
                "trade_history": str,      # 近期交易摘要
                "patterns": str,           # 识别的模式
                "symbol_warnings": dict,   # 按标的分类的警告
            }
        """
        # 缓存检查
        now = time.time()
        if self._cache and (now - self._cache_time) < self.cache_ttl:
            return self._cache

        result = {
            "lessons_text": self._load_lessons(),
            "lessons_count": self._count_lessons(),
            "memory_context": self._load_us_memory(),
            "trade_history": self._load_trade_history(),
            "patterns": self._extract_patterns(),
            "symbol_warnings": self._get_symbol_warnings(),
        }

        self._cache = result
        self._cache_time = now
        return result

    def to_prompt_section(self, max_total_chars: int = 8000) -> str:
        """
        格式化为辩论 prompt 的一个段落
        
        Args:
            max_total_chars: 总字符预算 (默认 8000), 防止超出 LLM 上下文窗口
        """
        ctx = self.inject()

        sections = ["═══════════════════════════════════════════"]
        sections.append("【长期记忆与历史教训 — 辩论必须参考】")
        sections.append("═══════════════════════════════════════════")
        sections.append("")
        budget_remaining = max_total_chars

        # Lessons (优先保留)
        if ctx["lessons_text"] and budget_remaining > 200:
            sections.append(f"## 活跃教训 ({ctx['lessons_count']} 条)")
            # 截断到预算内
            lesson_text = ctx["lessons_text"]
            if len(lesson_text) > budget_remaining - 500:
                lesson_text = lesson_text[:budget_remaining - 500] + "\n  ... (已截断)"
            sections.append(lesson_text)
            sections.append("")
            budget_remaining -= len(lesson_text) + 50

        # Memory
        if ctx["memory_context"] and budget_remaining > 200:
            sections.append("## 交易记忆 (us.json)")
            mem_text = ctx["memory_context"]
            if len(mem_text) > budget_remaining - 300:
                mem_text = mem_text[:budget_remaining - 300] + "\n... (已截断)"
            sections.append(mem_text)
            sections.append("")
            budget_remaining -= len(mem_text) + 30

        # Patterns
        if ctx["patterns"] and budget_remaining > 200:
            sections.append("## 识别的模式")
            pat_text = ctx["patterns"]
            if len(pat_text) > budget_remaining - 200:
                pat_text = pat_text[:budget_remaining - 200] + "\n... (已截断)"
            sections.append(pat_text)
            sections.append("")
            budget_remaining -= len(pat_text) + 20

        # Symbol warnings
        if ctx["symbol_warnings"] and budget_remaining > 200:
            sections.append("## 标的专项警告")
            for symbol, warnings in list(ctx["symbol_warnings"].items())[:5]:
                line = f"  ⚠️ {symbol}:"
                sections.append(line)
                budget_remaining -= len(line)
                if budget_remaining <= 100:
                    break
                for w in warnings[:2]:  # 每个标的最多 2 条
                    wline = f"    - {w}"
                    if len(wline) > budget_remaining:
                        wline = wline[:budget_remaining - 10] + "..."
                    sections.append(wline)
                    budget_remaining -= len(wline)
                if budget_remaining <= 50:
                    break
            sections.append("")

        # Trade history (最后)
        if ctx["trade_history"] and budget_remaining > 300:
            sections.append("## 近期交易摘要")
            trade_text = ctx["trade_history"]
            if len(trade_text) > budget_remaining - 200:
                trade_text = trade_text[:budget_remaining - 200] + "\n... (已截断)"
            sections.append(trade_text)
            sections.append("")

        return "\n".join(sections)

    # ─────────────────────────────────────────────────────
    #  数据加载方法
    # ─────────────────────────────────────────────────────

    def _load_lessons(self) -> str:
        """加载活跃教训"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT lesson_text, lesson_category, lesson_subcategory,
                       impact_score, confidence, symbols, created_at, validation_count
                FROM lessons
                WHERE status = 'active'
                  AND (expiry_date = '' OR expiry_date > ?)
                ORDER BY impact_score DESC
                LIMIT 20
            """, (datetime.now().isoformat(),)).fetchall()
            conn.close()

            if not rows:
                return "（无活跃教训）"

            lines = []
            for i, row in enumerate(rows, 1):
                text = row["lesson_text"] or ""
                category = row["lesson_category"] or ""
                impact = row["impact_score"] or 0
                symbols = row["symbols"] or "[]"
                confidence = row["confidence"] or 0

                # 截断过长的教训
                if len(text) > 200:
                    text = text[:197] + "..."

                try:
                    sym_list = json.loads(symbols)
                    sym_str = f" | 标的: {', '.join(sym_list[:3])}" if sym_list else ""
                except (json.JSONDecodeError, TypeError):
                    sym_str = ""

                lines.append(
                    f"  {i}. [{impact:.0f}分|{confidence:.0%}可信] {category}/{text}{sym_str}"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"[MemoryInjector] Lessons 加载失败: {e}")
            return f"（Lessons 加载失败: {e}）"

    def _count_lessons(self) -> int:
        """统计活跃教训数量"""
        try:
            conn = sqlite3.connect(self.db_path)
            count = conn.execute("""
                SELECT COUNT(*) FROM lessons
                WHERE status = 'active'
                  AND (expiry_date = '' OR expiry_date > ?)
            """, (datetime.now().isoformat(),)).fetchone()[0]
            conn.close()
            return count
        except sqlite3.Error:
            return 0

    def _load_us_memory(self) -> str:
        """加载 us.json 记忆"""
        try:
            if not Path(self.memory_file).exists():
                return "（无独立记忆文件）"

            with open(self.memory_file, 'r', encoding='utf-8') as f:
                memory = json.load(f)

            sections = []

            # 核心原则
            if memory.get("core_principle"):
                sections.append(f"  核心原则: {memory['core_principle']}")

            # 市场规则
            rules = memory.get("market_specific_rules", [])
            if rules:
                sections.append("  市场规则:")
                for rule in rules:
                    sections.append(f"    - {rule}")

            # 策略调整
            adjustments = memory.get("strategy_adjustments", [])
            if adjustments:
                sections.append("  近期策略调整:")
                for adj in adjustments[-5:]:  # 最近 5 条
                    sections.append(f"    - {adj}")

            # 标的教训
            symbol_lessons = memory.get("symbol_lessons", {})
            if symbol_lessons:
                sections.append("  标的专项教训:")
                for sym, lesson in list(symbol_lessons.items())[:5]:
                    sections.append(f"    - {sym}: {lesson}")

            # 性能追踪
            perf = memory.get("performance_tracking", {})
            if perf:
                sections.append(f"  性能: 交易={perf.get('total_trades', 0)} | "
                              f"胜率={perf.get('win_rate', 0):.0%} | "
                              f"最大回撤={perf.get('max_drawdown', 0):.0%}")

            return "\n".join(sections) if sections else "（记忆文件为空）"

        except Exception as e:
            logger.warning(f"[MemoryInjector] Memory 加载失败: {e}")
            return f"（Memory 加载失败: {e}）"

    def _load_trade_history(self) -> str:
        """加载近期交易历史摘要"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT symbol, direction, entry_price, exit_price,
                       realized_pnl, pnl_pct, entry_time, exit_time,
                       strategy_id, exit_reason
                FROM trade_records
                WHERE exit_time IS NOT NULL
                ORDER BY entry_time DESC
                LIMIT 20
            """).fetchall()

            if not rows:
                conn.close()
                return "（无历史交易记录）"

            wins = sum(1 for r in rows if (r["realized_pnl"] or 0) > 0)
            losses = sum(1 for r in rows if (r["realized_pnl"] or 0) < 0)
            total_pnl = sum(r["realized_pnl"] or 0 for r in rows)

            lines = [
                f"  近20笔: {wins}胜{losses}负 | 总盈亏=${total_pnl:.0f}",
                "  最近交易:",
            ]

            for r in rows[:10]:
                pnl = r["realized_pnl"] or 0
                pnl_pct = r["pnl_pct"] or 0
                sign = "+" if pnl >= 0 else ""
                direction = r["direction"] or "UNKNOWN"
                sym = r["symbol"] or "?"
                strat = r["strategy_id"] or "N/A"
                exit_r = r["exit_reason"] or "N/A"
                lines.append(
                    f"    {direction} {sym} | {sign}{pnl:.0f} ({sign}{pnl_pct:.1f}%) "
                    f"| {strat} | 退出: {exit_r}"
                )

            conn.close()
            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"[MemoryInjector] Trade history 加载失败: {e}")
            return f"（交易历史加载失败: {e}）"

    def _extract_patterns(self) -> str:
        """从历史数据中提取反复出现的模式"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            strat_stats = conn.execute("""
                SELECT strategy_id,
                       COUNT(*) as total,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       AVG(realized_pnl) as avg_pnl,
                       AVG(pnl_pct) as avg_pct
                FROM trade_records
                WHERE strategy_id IS NOT NULL AND exit_time IS NOT NULL
                GROUP BY strategy_id
                HAVING total >= 3
                ORDER BY avg_pct ASC
            """).fetchall()

            symbol_stats = conn.execute("""
                SELECT symbol,
                       COUNT(*) as total,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(realized_pnl) as total_pnl
                FROM trade_records
                WHERE symbol IS NOT NULL AND exit_time IS NOT NULL
                GROUP BY symbol
                HAVING total >= 2
                ORDER BY total_pnl ASC
            """).fetchall()

            conn.close()

            lines = []

            if strat_stats:
                lines.append("  策略表现 (按平均盈亏排序):")
                for s in strat_stats[:5]:
                    name = s["strategy_id"] or "N/A"
                    win_rate = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
                    lines.append(
                        f"    - {name}: {s['total']}笔 | "
                        f"胜率{win_rate:.0f}% | "
                        f"均盈亏${s['avg_pnl']:.0f} ({s['avg_pct']:.1f}%)"
                    )

            if symbol_stats:
                lines.append("  标的表现 (按总盈亏排序):")
                for s in symbol_stats[:5]:
                    sym = s["symbol"] or "?"
                    win_rate = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
                    lines.append(
                        f"    - {sym}: {s['total']}笔 | "
                        f"胜率{win_rate:.0f}% | "
                        f"总盈亏${s['total_pnl']:.0f}"
                    )

            return "\n".join(lines) if lines else "（无足够数据识别模式）"

        except Exception as e:
            logger.warning(f"[MemoryInjector] 模式提取失败: {e}")
            return f"（模式提取失败: {e}）"

    def _get_symbol_warnings(self) -> Dict[str, List[str]]:
        """按标的获取专项警告"""
        warnings = {}
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT lesson_text, symbols
                FROM lessons
                WHERE status = 'active' AND symbols IS NOT NULL
            """).fetchall()

            conn.close()

            for row in rows:
                symbols = []
                try:
                    symbols = json.loads(row["symbols"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue

                for sym in symbols:
                    if sym not in warnings:
                        warnings[sym] = []
                    warnings[sym].append(row["lesson_text"] or "")

            # 截断每个标的的警告数量
            for sym in warnings:
                warnings[sym] = warnings[sym][:3]

            return warnings

        except Exception as e:
            logger.warning(f"[MemoryInjector] 标的警告加载失败: {e}")
            return {}

    def invalidate_cache(self):
        """清除缓存，强制重新加载"""
        self._cache = None
        self._cache_time = 0


# =====================================================================
#  模块级单例
# =====================================================================

_injector: Optional[MemoryInjector] = None
_injector_lock = threading.Lock()


def get_memory_injector(db_path: str = None, memory_file: str = None) -> MemoryInjector:
    global _injector
    # P0 修复 (2026-05-03): 加锁防止并发初始化竞态
    with _injector_lock:
        if _injector is None:
            _injector = MemoryInjector(db_path=db_path, memory_file=memory_file)
    return _injector


def reset_memory_injector():
    global _injector
    with _injector_lock:
        _injector = None
