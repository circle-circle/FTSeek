"""
记忆管理器 — LRU + 重要性评分 + 归档 + 次日注入

替代原来的简单 JSON append，实现:
- 重要性评分 (impact_score) 排序
- LRU 裁剪 (保留最近 N 条 + 高分优先)
- 归档机制 (裁剪的条目不删除，归档到单独文件)
- 次日注入 (next_day_hints → 次日盘前 prompt)
"""
import json
import logging
import os
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek_us.memory_manager")


class MemoryManager:
    """记忆管理器"""
    
    MAX_HISTORICAL = 20
    MAX_TAGGED = 50
    MAX_ADJUSTMENTS = 15
    MIN_IMPACT_SCORE = 0.3
    
    def __init__(self, memory_dir: str = None):
        self.memory_dir = memory_dir or os.path.join(
            os.path.expanduser("~"), ".hermes", "ftseek", "memory"
        )
        os.makedirs(self.memory_dir, exist_ok=True)
        
        self.memory_file = os.path.join(self.memory_dir, "us.json")
        self.archive_file = os.path.join(self.memory_dir, "us_archived.json")
        self._lock = threading.Lock()
        
        self.memory = self._load_memory()
    
    def _load_memory(self) -> Dict:
        """加载记忆"""
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Memory] 加载失败: {e}")
        
        return self._default_memory()
    
    def _default_memory(self) -> Dict:
        return {
            "market": "美股",
            "core_principle": "稳定赚钱 > 激进收益",
            "market_specific_rules": [
                "1. 永远先保本，再考虑盈利",
                "2. 单日亏损超 3% 立即停手",
                "3. 连续 3 笔亏损，强制休息",
                "4. 计划外的交易需要 2 倍确认",
                "5. 盈利时要拿得住，亏损时要跑得快",
                "6. 不要试图抓住每一波行情",
                "7. 美股受宏观数据 (CPI/非农/FOMC) 影响大",
                "8. 注意盘前/盘后交易量和价差",
                "9. PDT 规则限制 (账户<$25k 时 5 天 3 次)",
                "10. 资金安全第一，收益率第二",
            ],
            "historical_lessons": [],
            "tagged_lessons": [],
            "strategy_adjustments": [],
            "symbol_lessons": {},
            "performance_tracking": {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "last_30_days_pnl": 0.0,
            },
            "next_day_hints": {},
            "last_updated": datetime.now().isoformat(),
        }
    
    def save(self):
        """保存记忆 (线程安全)"""
        with self._lock:
            self.memory["last_updated"] = datetime.now().isoformat()
            try:
                with open(self.memory_file, 'w', encoding='utf-8') as f:
                    json.dump(self.memory, f, ensure_ascii=False, indent=2)
                logger.info(f"[Memory] 已保存 (tagged={len(self.memory.get('tagged_lessons', []))})")
            except Exception as e:
                logger.error(f"[Memory] 保存失败: {e}")
    
    def update_from_attribution(self, attribution: Dict):
        """从盘后归因更新记忆 (线程安全)"""
        with self._lock:
            mu = attribution.get("memory_updates", {})
            
            # 更新 symbol_lessons
            for symbol, lesson in mu.get("symbol_lessons", {}).items():
                self.memory.setdefault("symbol_lessons", {})[symbol] = lesson
            
            # 添加 tagged_lessons (带重要性评分)
            for item in attribution.get("trade_attribution", []):
                lesson = {
                    "symbol": item.get("symbol"),
                    "date": attribution.get("date"),
                    "pnl_pct": item.get("pnl_pct"),
                    "lesson": item.get("lesson", ""),
                    "plan_aligned": item.get("plan_aligned"),
                    "impact_score": abs(item.get("pnl_pct", 0)) / 10.0,
                    "created_at": datetime.now().isoformat(),
                    "last_used": None,
                }
                self.memory.setdefault("tagged_lessons", []).append(lesson)
            
            # 添加 strategy_adjustments
            for adj in mu.get("strategy_adjustments", []):
                self.memory.setdefault("strategy_adjustments", []).append({
                    "adjustment": adj,
                    "date": attribution.get("date"),
                })
            
            # 添加 historical_lessons
            for lesson in mu.get("historical_lessons", []):
                self.memory.setdefault("historical_lessons", []).append(lesson)
            
            # 更新性能追踪
            perf = mu.get("performance", {})
            tracking = self.memory.setdefault("performance_tracking", {})
            if "win_rate" in perf:
                tracking["win_rate"] = perf["win_rate"]
            if "avg_pnl" in perf:
                tracking["avg_pnl"] = perf["avg_pnl"]
            
            # 更新次日 hints
            self.memory["next_day_hints"] = attribution.get("next_day_hints", {})
            
            # 裁剪
            self._prune_locked()
        
        # 保存 (在锁外，但 memory 已被锁保护)
        self.save()
    
    def _prune_locked(self):
        """裁剪 (调用方必须持有锁)"""
        # 裁剪 tagged_lessons
        lessons = self.memory.get("tagged_lessons", [])
        if len(lessons) > self.MAX_TAGGED:
            lessons.sort(
                key=lambda x: (
                    x.get("impact_score", 0),
                    x.get("created_at", ""),
                ),
                reverse=True,
            )
            
            keep = lessons[:self.MAX_TAGGED]
            archive = lessons[self.MAX_TAGGED:]
            
            self.memory["tagged_lessons"] = keep
            
            # 归档 (在锁内完成，避免释放锁后 memory 被修改)
            self._archive_lessons(archive)
            logger.info(f"[Memory] 裁剪 tagged_lessons: {len(lessons)} → {len(keep)} (归档 {len(archive)})")
        
        # 裁剪 historical_lessons
        hist = self.memory.get("historical_lessons", [])
        if len(hist) > self.MAX_HISTORICAL:
            self.memory["historical_lessons"] = hist[-self.MAX_HISTORICAL:]
        
        # 裁剪 strategy_adjustments
        adj = self.memory.get("strategy_adjustments", [])
        if len(adj) > self.MAX_ADJUSTMENTS:
            self.memory["strategy_adjustments"] = adj[-self.MAX_ADJUSTMENTS:]
    
    def _archive_lessons(self, lessons: List[Dict]):
        """归档裁剪的教训"""
        archived = []
        if os.path.exists(self.archive_file):
            try:
                with open(self.archive_file, 'r', encoding='utf-8') as f:
                    archived = json.load(f)
            except Exception:
                pass
        
        archived.extend(lessons)
        
        # 限制归档文件大小
        if len(archived) > 500:
            archived = archived[-500:]
        
        try:
            with open(self.archive_file, 'w', encoding='utf-8') as f:
                json.dump(archived, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Memory] 归档失败: {e}")
    
    def get_context_for_prompt(self) -> str:
        """格式化记忆为 prompt 上下文"""
        lines = []
        
        # 核心原则
        lines.append("【核心原则】")
        lines.append(self.memory.get("core_principle", ""))
        
        # 市场规则
        rules = self.memory.get("market_specific_rules", [])
        if rules:
            lines.append("\n【市场规则】")
            lines.extend(rules[:10])
        
        # 标的教训
        symbol_lessons = self.memory.get("symbol_lessons", {})
        if symbol_lessons:
            lines.append("\n【标的教训】")
            for symbol, lesson in list(symbol_lessons.items())[-10:]:
                lines.append(f"  {symbol}: {lesson}")
        
        # 策略调整
        adjustments = self.memory.get("strategy_adjustments", [])
        if adjustments:
            lines.append("\n【近期策略调整】")
            for adj in list(adjustments)[-5:]:
                if isinstance(adj, dict):
                    lines.append(f"  [{adj.get('date', '')}] {adj.get('adjustment', '')}")
                else:
                    lines.append(f"  {adj}")
        
        # 历史教训 (最近的)
        hist = self.memory.get("historical_lessons", [])
        if hist:
            lines.append("\n【历史教训】")
            for lesson in list(hist)[-5:]:
                lines.append(f"  - {lesson}")
        
        # 性能追踪
        perf = self.memory.get("performance_tracking", {})
        if perf:
            lines.append("\n【性能追踪】")
            lines.append(f"  胜率: {perf.get('win_rate', 0):.0%}")
            lines.append(f"  平均盈亏: {perf.get('avg_pnl', 0):.1%}")
            lines.append(f"  最大回撤: {perf.get('max_drawdown', 0):.0%}")
        
        return "\n".join(lines)
    
    def get_next_day_hints(self) -> Dict:
        """获取次日注入的 hints"""
        hints = self.memory.get("next_day_hints", {})
        
        # 如果 hints 超过 24h，标记过期
        updated = self.memory.get("last_updated", "")
        if updated:
            try:
                last_update = datetime.fromisoformat(updated)
                if datetime.now() - last_update > timedelta(hours=24):
                    logger.info("[Memory] 次日 hints 已过期")
                    return {}
            except Exception:
                pass
        
        return hints
    
    def get(self, key: str, default=None):
        return self.memory.get(key, default)
    
    def set(self, key: str, value: Any):
        self.memory[key] = value


# =====================================================================
#  模块级单例
# =====================================================================

_memory_manager: Optional[MemoryManager] = None


def get_memory_manager(memory_dir: str = None) -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager(memory_dir=memory_dir)
    return _memory_manager
