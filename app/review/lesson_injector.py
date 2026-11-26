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


# ═══════════════════════════════════════════════════════════════
#  批次0 (DEB-20260904 §10.2): lessons 生命周期 4 列幂等迁移 SSOT
#  status 为既有列 (legacy 小写 'active'/'inactive' 语义保留不变);
#  recurrence_count / last_triggered_at / cost_since 三新列由
#  scripts/daily_violation_scan.py 维护 (唯一写入方), 本模块只保证列存在,
#  使注入端排序 (闸2: recurrence×cost) 在任何初始化顺序下都不会撞缺列。
# ═══════════════════════════════════════════════════════════════
LESSONS_LIFECYCLE_COLUMNS = (
    ("status", "TEXT DEFAULT 'active'"),
    ("recurrence_count", "INTEGER DEFAULT 0"),
    ("last_triggered_at", "TEXT DEFAULT ''"),
    ("cost_since", "REAL DEFAULT 0.0"),
)


def ensure_lessons_lifecycle_columns(conn) -> list:
    """幂等扩列, 返回实际新增的列名列表 (调用方负责 commit)。"""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(lessons)").fetchall()}
    added = []
    for col, decl in LESSONS_LIFECYCLE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE lessons ADD COLUMN {col} {decl}")
            added.append(col)
    return added


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
            ensure_lessons_lifecycle_columns(conn)  # 批次0 幂等扩列 (DEB-20260904 §10.2)
            conn.commit()
        finally:
            conn.close()
    
    def load_active_lessons(self) -> List[Dict]:
        """加载所有活跃教训"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d')
            # 批次0 闸1+闸2 (DEB-20260904 §10.2): status 纳入扫描器晋升的 'ACTIVE'
            # (PENDING/RETIRED 不注入); 排序 recurrence×|cost| 优先 — legacy 行两列为
            # 0/NULL → 乘积 0 → tie-break impact_score DESC, 旧教训次序与改前逐行等价。
            # ABS 修正 (2026-09-05, team-lead 核验): cost_since = −Σpnl_net
            # (正=违例笔净亏即真烧钱, 负=违例笔净盈利即矛盾信号 — 生产库逐笔裁决确认)。
            # 带符号排序会把净亏教训 (正×正=正? 不: 初版键=n×cost 对负 cost 得负积)
            # 沉到 legacy 0 乘积之下, 与 §10.1 "高复发×高成本顶掉低质模板" 意图相反;
            # 排序取 ABS (净赚的复发教训同样上浮), 符号保留在列中, 展示层如实标注。
            rows = conn.execute(
                "SELECT * FROM lessons WHERE status IN ('active', 'ACTIVE') "
                "AND (expiry_date = '' OR expiry_date > ?) "
                "ORDER BY COALESCE(recurrence_count, 0) * ABS(COALESCE(cost_since, 0.0)) DESC, "
                "impact_score DESC",
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
        
        只以已平仓的净损益验证；未平仓、无观察及非法时间均不算支持证据。
        
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
                    observed_premarket = False
                    premarket_trades = []
                    for trade in today_trades:
                        if not trade.get('entry_time'):
                            continue
                        is_premarket = self._is_premarket(trade.get('entry_time', ''))
                        if is_premarket is None:
                            continue
                        if is_premarket:
                            observed_premarket = True
                        if is_premarket and trade.get('exit_price') and trade.get('exit_price') > 0:
                            premarket_trades.append(trade)

                    if not observed_premarket or not premarket_trades:
                        # 无反例或未结算都不是支持证据，confidence/expiry/计数原样保留。
                        pass
                    else:
                        premarket_pnl = sum(self._trade_net_pnl(t) for t in premarket_trades)
                        if premarket_pnl < 0:
                            new_confidence = min(1.0, old_confidence + 0.1)
                            validated += 1
                            should_update = True
                        else:
                            new_confidence = max(0.1, old_confidence - 0.15)
                            invalidated += 1
                            should_update = True
                
                elif subcat == 'high_signal_loss':
                    # 检查今天高信号交易的表现
                    high_signal_trades = [
                        t for t in today_trades
                        if (t.get('signal_strength', 0) or 0) >= 60
                        and t.get('exit_price') and t.get('exit_price') > 0
                    ]
                    
                    if high_signal_trades:
                        avg_pnl = sum(self._trade_net_pnl(t) for t in high_signal_trades) / len(high_signal_trades)
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
    
    @staticmethod
    def _trade_net_pnl(trade: Dict) -> float:
        net = trade.get('pnl_net')
        if net is not None:
            return float(net)
        return float(trade.get('realized_pnl', 0) or 0) - float(trade.get('fees_est', 0) or 0)

    def _is_premarket(self, entry_time: str) -> Optional[bool]:
        """带时区转ET；无时区按生产入口的ET，不将其错置到下一交易日。"""
        if not entry_time:
            return None
        try:
            from app.utils.timezone import ET
            from pytz.exceptions import InvalidTimeError
            entry_dt = datetime.fromisoformat(str(entry_time).replace('Z', '+00:00'))
            et_dt = (ET.localize(entry_dt, is_dst=None) if entry_dt.utcoffset() is None
                     else entry_dt.astimezone(ET))
            et_minutes = et_dt.hour * 60 + et_dt.minute
            return 4 * 60 <= et_minutes < 9 * 60 + 30
        except (ValueError, TypeError, InvalidTimeError):
            return None
    
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

_lesson_injector_singleton: Optional[LessonInjector] = None


def get_lesson_injector() -> LessonInjector:
    """返回进程级 LessonInjector 单例 (懒加载)。"""
    global _lesson_injector_singleton
    if _lesson_injector_singleton is None:
        _lesson_injector_singleton = LessonInjector()
    return _lesson_injector_singleton


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
