"""
FTSeek-US 教训注入器 (LessonInjector v1.0)

盘前加载活跃教训，注入到:
1. LLM 盘前作战计划 prompt
2. RiskEngine 预交易检查
3. VotingCoordinator 信心度调整

设计原则:
  - 零 LLM 成本: 纯规则注入
  - 可验证: 每条教训都有 expiry_date 和 confidence
  - 自动衰减: 过期教训自动失效
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger("ftseek_us.lesson_injector")


class LessonInjector:
    """
    从 lessons DB 加载活跃教训，生成结构化注入提示
    
    使用场景:
    1. generate_battle_plan() → 注入 LLM prompt
    2. pre_trade_check() → 生成交易约束
    3. voting_coordinator → 信心度调整
    """
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
        self.db_path = db_path
        self._init_db()  # 防御性初始化 — 确保 lessons 表存在 (独立于 ExperienceAccumulator)
    
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """防御性初始化: 确保 lessons 表存在 (独立于 ExperienceAccumulator)"""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lessons (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_text     TEXT NOT NULL,
                    lesson_category TEXT NOT NULL,
                    lesson_subcategory TEXT NOT NULL,
                    impact_score    REAL DEFAULT 0.0,
                    confidence      REAL DEFAULT 0.5,
                    symbols         TEXT DEFAULT '[]',
                    source_type     TEXT DEFAULT 'auto',
                    status          TEXT DEFAULT 'active',
                    expiry_date     TEXT DEFAULT '',
                    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                    evidence        TEXT DEFAULT '{}',
                    validation_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(lesson_category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_expiry ON lessons(expiry_date)")
            conn.commit()
        finally:
            conn.close()
    
    def load_active_lessons(self) -> List[Dict]:
        """加载所有活跃教训"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d')
            rows = conn.execute(
                "SELECT * FROM lessons WHERE status = 'active' AND (expiry_date = '' OR expiry_date > ?) ORDER BY impact_score DESC",
                (now,)
            ).fetchall()
            
            lessons = []
            for r in rows:
                lesson = dict(r)
                lesson['symbols'] = json.loads(lesson['symbols']) if lesson['symbols'] else []
                lesson['evidence'] = json.loads(lesson['evidence']) if lesson['evidence'] else {}
                lessons.append(lesson)
            
            return lessons
        finally:
            conn.close()
    
    def generate_battle_plan_injection(self) -> str:
        """
        生成注入到 LLM 盘前作战计划的文本
        
        Returns:
            str: 格式化的教训提示文本
        """
        lessons = self.load_active_lessons()
        
        if not lessons:
            return "【历史教训】无活跃教训，今日正常执行"
        
        lines = ["【历史教训】(从昨日复盘自动提取，请参考)"]
        
        # 按类别分组
        by_category = {}
        for l in lessons:
            cat = l.get('lesson_category', 'other')
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(l)
        
        category_names = {
            'trading': '📊 交易策略',
            'exit': '🚪 退出策略',
            'risk': '⚠️ 风控合规',
            'signal': '📡 信号质量',
            'timing': '⏰ 时段选择',
        }
        
        for cat, cat_lessons in by_category.items():
            cat_name = category_names.get(cat, '📋 其他')
            lines.append(f"\n{cat_name}:")
            for l in cat_lessons[:3]:  # 每个类别最多显示 3 条
                symbols = f" ({', '.join(l['symbols'][:3])})" if l['symbols'] else ""
                confidence = f" [置信度: {l['confidence']:.0%}]" if l.get('confidence') else ""
                expiry = f" [有效期至: {l['expiry_date']}]" if l.get('expiry_date') else ""
                lines.append(f"  • {l['lesson_text'][:120]}{symbols}{confidence}{expiry}")
        
        return '\n'.join(lines)
    
    def generate_trade_constraints(self, symbol: str = None, session: str = None) -> Dict:
        """
        生成针对特定标的/时段的交易约束
        
        Args:
            symbol: 标的代码 (可选)
            session: 交易时段 (premarket/opening_drive/morning/etc)
        
        Returns:
            Dict: {
                'allow': bool,              # 是否允许交易
                'reason': str,              # 原因
                'extra_confidence': float,  # 额外信心度要求
                'max_position_pct': float,  # 最大仓位占比 (1.0=100%)
            }
        """
        lessons = self.load_active_lessons()
        
        constraints = {
            'allow': True,
            'reason': '',
            'extra_confidence': 0.0,
            'max_position_pct': 1.0,
        }
        
        for l in lessons:
            subcat = l.get('lesson_subcategory', '')
            symbols = l.get('symbols', [])
            confidence = l.get('confidence', 0.5)
            
            # 盘前亏损教训 → 禁止盘前交易
            if subcat == 'premarket_loss' and session == 'premarket':
                constraints['allow'] = False
                constraints['reason'] += f"活跃教训: 盘前入场亏损历史 — {l['lesson_text'][:80]}; "
                break
            
            # 高信号亏损 → 需要额外信心度
            if subcat == 'high_signal_loss' and (not symbol or symbol in symbols):
                constraints['extra_confidence'] += 0.1 * confidence
                constraints['reason'] += f"需额外确认: {l['lesson_text'][:60]}; "
            
            # 止损参数过宽 → 降低最大仓位
            if subcat in ('preclose_loss', 'exit_params'):
                constraints['max_position_pct'] = min(constraints['max_position_pct'], 0.7)
                constraints['reason'] += f"仓位收紧: {l['lesson_text'][:60]}; "
            
            # 不合理交易占比高 → 整体收紧
            if subcat == 'high_unreasonable_ratio':
                constraints['max_position_pct'] = min(constraints['max_position_pct'], 0.6)
                constraints['extra_confidence'] += 0.1
                constraints['reason'] += "整体收紧: 历史不合理交易占比过高; "

            # P0-fix 2026-05-04: 信号管线静默失败警告 → 额外确认要求
            if subcat == 'signal_pipeline_issue' and (not symbol or symbol in symbols):
                constraints['extra_confidence'] += 0.05 * confidence
                constraints['reason'] += f"信号管线告警: {l['lesson_text'][:60]}; "
        
        # 清理 reason 末尾的分号
        constraints['reason'] = constraints['reason'].rstrip('; ')
        
        return constraints
    
    def validate_lessons(self, today_trades: List[Dict]) -> Dict:
        """
        验证活跃教训的有效性
        
        逻辑:
        - 如果教训说"避免盘前交易"，今天没有盘前交易 → 教训有效 → 延长有效期
        - 如果教训说"避免盘前交易"，今天盘前交易亏损 → 教训有效 → 提升置信度
        - 如果教训说"避免盘前交易"，今天盘前交易盈利 → 教训无效 → 降低置信度
        
        Args:
            today_trades: 今日交易列表 (来自 trade_records)
        
        Returns:
            Dict: {
                'validated': int,    # 验证通过的教训数
                'invalidated': int,  # 验证失败的教训数
                'updated': int,      # 更新的教训数
            }
        """
        conn = self._get_conn()
        try:
            updated = 0
            validated = 0
            invalidated = 0
            
            # 获取活跃教训
            now = datetime.now().strftime('%Y-%m-%d')
            rows = conn.execute(
                "SELECT * FROM lessons WHERE status = 'active' AND (expiry_date = '' OR expiry_date > ?)",
                (now,)
            ).fetchall()
            
            for r in rows:
                lesson = dict(r)
                subcat = lesson.get('lesson_subcategory', '')
                old_confidence = lesson.get('confidence', 0.5)
                new_confidence = old_confidence
                should_update = False
                
                # 验证规则
                if subcat == 'premarket_loss':
                    # 检查今天是否有盘前交易
                    premarket_trades = [
                        t for t in today_trades
                        if self._is_premarket(t.get('entry_time', ''))
                    ]
                    
                    if not premarket_trades:
                        # 没有盘前交易 → 教训有效
                        new_confidence = min(1.0, old_confidence + 0.05)
                        validated += 1
                        should_update = True
                    else:
                        # 有盘前交易，检查盈亏
                        premarket_pnl = sum(t.get('realized_pnl', 0) or 0 for t in premarket_trades)
                        if premarket_pnl < 0:
                            # 盘前亏损 → 教训正确
                            new_confidence = min(1.0, old_confidence + 0.1)
                            validated += 1
                            should_update = True
                        else:
                            # 盘前盈利 → 教训可能不正确
                            new_confidence = max(0.1, old_confidence - 0.15)
                            invalidated += 1
                            should_update = True
                
                elif subcat == 'high_signal_loss':
                    # 检查今天高信号交易的表现
                    high_signal_trades = [
                        t for t in today_trades
                        if (t.get('signal_strength', 0) or 0) >= 60
                    ]
                    
                    if high_signal_trades:
                        avg_pnl = sum(t.get('realized_pnl', 0) or 0 for t in high_signal_trades) / len(high_signal_trades)
                        if avg_pnl < 0:
                            new_confidence = min(1.0, old_confidence + 0.05)
                            validated += 1
                            should_update = True
                        else:
                            new_confidence = max(0.1, old_confidence - 0.1)
                            invalidated += 1
                            should_update = True
                
                # 更新 DB
                if should_update:
                    new_expiry = (datetime.now() + timedelta(days=14 if new_confidence >= 0.7 else 7)).strftime('%Y-%m-%d')
                    conn.execute(
                        "UPDATE lessons SET confidence = ?, expiry_date = ?, validation_count = validation_count + 1 WHERE id = ?",
                        (new_confidence, new_expiry, lesson['id'])
                    )
                    updated += 1
            
            conn.commit()
            return {
                'validated': validated,
                'invalidated': invalidated,
                'updated': updated,
            }
        finally:
            conn.close()
    
    def _is_premarket(self, entry_time: str) -> bool:
        """判断是否为盘前入场"""
        if not entry_time:
            return False
        try:
            from datetime import timedelta
            entry_dt = datetime.fromisoformat(entry_time)
            et_hour = (entry_dt.hour - 13) % 24
            return 4 <= et_hour < 9
        except (ValueError, TypeError):
            return False
    
    def get_stats(self) -> Dict:
        """获取教训统计"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d')
            
            total = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE status = 'active' AND (expiry_date = '' OR expiry_date > ?)",
                (now,)
            ).fetchone()[0]
            expired = conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE expiry_date != '' AND expiry_date <= ?",
                (now,)
            ).fetchone()[0]
            validated = conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE validation_count > 0"
            ).fetchone()[0]
            
            # 按类别统计
            by_category = {}
            rows = conn.execute(
                "SELECT lesson_category, COUNT(*) as cnt FROM lessons WHERE status = 'active' GROUP BY lesson_category"
            ).fetchall()
            for r in rows:
                by_category[r['lesson_category']] = r['cnt']
            
            return {
                'total': total,
                'active': active,
                'expired': expired,
                'validated': validated,
                'by_category': by_category,
            }
        finally:
            conn.close()

    def save_lesson(
        self,
        category: str,
        title: str,
        description: str,
        action_required: str = "",
        impact_score: float = 0.5,
        expiry_days: int = 30,
        source: str = "daily_review",
    ) -> str:
        """保存一条教训到 lessons DB
        
        Args:
            category: 教训类别 (e.g., 'entry_timing', 'exit_strategy', 'risk_management')
            title: 教训标题 → 存入 lesson_text
            description: 详细描述 → 存入 evidence JSON
            action_required: 需要采取的行动 → 存入 evidence JSON
            impact_score: 影响评分 (0-1)
            expiry_days: 过期天数
            source: 来源 → 存入 source_type
        
        Returns:
            lesson_id (内部自增 id 作为字符串返回)
        """
        expiry_date = (datetime.now() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
        now = datetime.now().isoformat()
        
        # 将 title/description/action_required 编码到可用的列中
        lesson_text = title
        evidence_json = json.dumps({
            "description": description,
            "action_required": action_required,
            "source": source,
        })
        
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                INSERT INTO lessons
                (lesson_category, lesson_subcategory, lesson_text,
                 impact_score, confidence, status, source_type, expiry_date,
                 created_at, symbols, evidence)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, '[]', ?)
            """, (
                category, "", lesson_text,
                impact_score, 0.5, source, expiry_date, now, evidence_json,
            ))
            conn.commit()
            lesson_id = str(cursor.lastrowid)
            logger.info(f"[LessonInjector] 教训已保存: id={lesson_id} [{category}] {title}")
            return lesson_id
        except Exception as e:
            conn.rollback()
            logger.error(f"[LessonInjector] 教训保存失败: {e}")
            raise
        finally:
            conn.close()
    
    def archive_lesson(self, lesson_id: str, reason: str = "resolved"):
        """归档一条教训 (标记为 inactive)
        
        Args:
            lesson_id: 内部自增 id (字符串或整数)
        """
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE lessons SET status = 'inactive'
                WHERE id = ?
            """, (int(lesson_id),))
            conn.commit()
            logger.info(f"[LessonInjector] 教训已归档: id={lesson_id} (原因: {reason})")
        except Exception as e:
            conn.rollback()
            logger.error(f"[LessonInjector] 教训归档失败: {e}")
            raise
        finally:
            conn.close()


# ============================================================================
#  公共接口
# ============================================================================

def get_next_day_hints() -> Dict:
    """获取次日交易提示 — P0-5 修复: 不再依赖不存在的 ExperienceAccumulator"""
    injector = get_lesson_injector()
    lessons = injector.load_active_lessons()
    
    hints = {
        "total_lessons": len(lessons),
        "hints": [],
        "generated_at": datetime.now().isoformat(),
    }
    
    for l in lessons[:5]:  # 最多 5 条
        # 从 evidence JSON 中提取 description/action_required (save_lesson 存入)
        evidence = l.get("evidence", {})
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (json.JSONDecodeError, TypeError):
                evidence = {}
        
        hints["hints"].append({
            "category": l.get("lesson_category", ""),
            "title": l.get("lesson_text", ""),
            "action": evidence.get("action_required", evidence.get("description", "")),
            "impact": l.get("impact_score", 0.5),
        })
    
    return hints
