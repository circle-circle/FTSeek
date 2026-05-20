"""
FTSeek-US 经验积累引擎 (ExperienceAccumulator v1.0)

从复盘报告中自动提取教训，持久化到 DB，供次日交易决策使用。

架构:
  日评报告 → 规则提取 → 去重 → lessons DB → 次日提示
      ↓
  辩论结论 → 结构化教训 → lessons DB (高置信度)
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek_us.experience_accumulator")


# ============================================================================
#  数据类
# ============================================================================

@dataclass
class Lesson:
    """结构化教训"""
    lesson_text: str                # 人类可读的教训文本
    lesson_category: str            # 分类: trading/exit/risk/signal/timing/sector
    lesson_subcategory: str         # 子类: premarket_loss/high_signal_loss/preclose_loss/etc
    impact_score: float = 0.0       # 影响力评分 (基于 PnL 加权)
    confidence: float = 0.5         # 置信度 (0-1)
    symbols: List[str] = field(default_factory=list)  # 涉及的标的
    source_type: str = "auto"       # auto/debate/weekly_llm/manual
    status: str = "active"          # active/validated/invalidated/expired
    expiry_date: str = ""           # 过期日期
    created_at: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    validation_count: int = 0       # 验证次数

    def to_dict(self) -> Dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ============================================================================
#  ExperienceAccumulator: 经验积累引擎
# ============================================================================

class ExperienceAccumulator:
    """
    从复盘报告中提取教训并持久化
    
    提取来源:
    1. trade_analyses (逐笔交易分析)
    2. error_patterns (错误模式)
    3. risk_compliance (风险违规)
    4. verdict_summary (判定分布)
    5. 辩论交易 (needs_debate=True)
    """
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db")
        self.db_path = db_path
        self._init_db()
    
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    
    def _init_db(self):
        """创建 lessons 表 (幂等)"""
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
                    symbols         TEXT DEFAULT '[]',       -- JSON array
                    source_type     TEXT DEFAULT 'auto',
                    status          TEXT DEFAULT 'active',
                    expiry_date     TEXT DEFAULT '',
                    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                    evidence        TEXT DEFAULT '{}',       -- JSON
                    validation_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(lesson_category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_expiry ON lessons(expiry_date)")
            conn.commit()
        finally:
            conn.close()
    
    def accumulate(self, report: Dict) -> Dict:
        """
        从日评报告提取并积累教训
        
        Args:
            report: daily_review generate() 输出的完整报告
        
        Returns:
            Dict: {
                'lessons_extracted': int,
                'lessons_persisted': int,
                'next_day_hints': Dict,  # 次日交易提示
                'lesson_count': int,     # 活跃教训总数
            }
        """
        lessons = []
        
        # 1. 从 trade_analyses 提取
        trade_analyses = report.get('trade_analyses', [])
        lessons.extend(self._extract_from_trades(trade_analyses))
        
        # 2. 从 verdict_summary 提取
        verdict_summary = report.get('verdict_summary', {})
        lessons.extend(self._extract_from_verdicts(verdict_summary, trade_analyses))
        
        # 3. 从 debate_trades 提取
        debate_trades = report.get('debate_trades', [])
        lessons.extend(self._extract_from_debates(debate_trades))
        
        # 4. 从 error_patterns 提取
        error_patterns = report.get('error_patterns', {})
        lessons.extend(self._extract_from_errors(error_patterns))
        
        # 5. 从 risk_compliance 提取
        dimensions = report.get('dimensions', {})
        risk = dimensions.get('risk_compliance', {})
        lessons.extend(self._extract_from_risk(risk))
        
        # 去重并持久化
        lessons = self._deduplicate(lessons)
        persisted = self._persist(lessons)
        
        # 生成次日提示
        next_day_hints = self._generate_next_day_hints()
        
        logger.info(
            f"[ExperienceAcc] 提取={len(lessons)} 持久化={persisted} "
            f"活跃={next_day_hints['lesson_count']}"
        )
        
        return {
            'lessons_extracted': len(lessons),
            'lessons_persisted': persisted,
            'next_day_hints': next_day_hints,
            'lesson_count': next_day_hints['lesson_count'],
        }
    
    def _extract_from_trades(self, trades: List[Dict]) -> List[Lesson]:
        """从逐笔交易分析提取教训"""
        lessons = []
        
        for t in trades:
            verdict = t.get('verdict', '')
            issues = t.get('issues', [])
            pnl = t.get('realized_pnl', 0)
            symbol = t.get('symbol', '')
            exit_reason = t.get('exit_reason', '')
            
            # 规则 1: 盘前入场亏损
            if any("盘前入场" in i for i in issues) and pnl < 0:
                impact = abs(pnl)
                lessons.append(Lesson(
                    lesson_text=f"盘前入场 {symbol} 亏损 ${abs(pnl):.2f} ({pnl*100/t.get('entry_price',1):.2f}%) — 避免盘前交易",
                    lesson_category="timing",
                    lesson_subcategory="premarket_loss",
                    impact_score=impact,
                    confidence=0.7,
                    symbols=[symbol],
                    source_type="auto",
                    expiry_date=(datetime.now() + timedelta(days=14)).strftime('%Y-%m-%d'),
                    evidence={'trade_id': t.get('trade_id'), 'entry_time': t.get('entry_time')},
                ))
            
            # 规则 2: 高信号强度但亏损
            strength = t.get('signal_strength', 0)
            if strength >= 60 and pnl < 0:
                lessons.append(Lesson(
                    lesson_text=f"高信号强度 (strength={strength}) {symbol} 仍亏损 ${abs(pnl):.2f} — 信号可靠性需验证",
                    lesson_category="signal",
                    lesson_subcategory="high_signal_loss",
                    impact_score=abs(pnl),
                    confidence=0.6,
                    symbols=[symbol],
                    source_type="auto",
                    expiry_date=(datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'),
                    evidence={'trade_id': t.get('trade_id'), 'strength': strength},
                ))
            
            # 规则 3: 盘末强平亏损
            if exit_reason == 'PRE_CLOSE_EXIT' and pnl < 0:
                holding = t.get('holding_hours', 0)
                if holding >= 5:
                    lessons.append(Lesson(
                        lesson_text=f"{symbol} 持仓 {holding:.1f}h 全程未触发止损/止盈 — 止损参数过宽",
                        lesson_category="exit",
                        lesson_subcategory="preclose_loss",
                        impact_score=abs(pnl),
                        confidence=0.8 if holding >= 6 else 0.6,
                        symbols=[symbol],
                        source_type="auto",
                        expiry_date=(datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'),
                        evidence={'trade_id': t.get('trade_id'), 'holding_hours': holding},
                    ))
        
        return lessons
    
    def _extract_from_verdicts(self, verdict_summary: Dict, trades: List[Dict]) -> List[Lesson]:
        """从判定分布提取教训"""
        lessons = []
        total = len(trades)
        if total == 0:
            return lessons
        
        unreasonable = verdict_summary.get('不合理', 0)
        reasonable = verdict_summary.get('合理', 0)
        
        # 不合理占比过高
        if total >= 3 and unreasonable / total > 0.5:
            lessons.append(Lesson(
                lesson_text=f"今日 {unreasonable}/{total} ({unreasonable/total*100:.0f}%) 交易判定为不合理 — 需系统性审查",
                lesson_category="trading",
                lesson_subcategory="high_unreasonable_ratio",
                impact_score=unreasonable * 100,
                confidence=0.9,
                source_type="auto",
                expiry_date=(datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d'),
                evidence={'unreasonable_count': unreasonable, 'total': total},
            ))
        
        return lessons
    
    def _extract_from_debates(self, debate_trades: List[Dict]) -> List[Lesson]:
        """从辩论交易提取教训"""
        lessons = []
        
        for dt in debate_trades:
            reason = dt.get('debate_reason', '')
            symbol = dt.get('symbol', '')
            
            if '盘前入场' in reason and dt.get('realized_pnl', 0) > 0:
                # 盘前盈利 — 需要辩论确认是策略还是运气
                lessons.append(Lesson(
                    lesson_text=f"{symbol} 盘前入场盈利 ${dt['realized_pnl']:.2f} — 待辩论确认是策略有效还是运气",
                    lesson_category="timing",
                    lesson_subcategory="premarket_profit_debate",
                    impact_score=dt.get('realized_pnl', 0),
                    confidence=0.3,  # 低置信度，需要辩论确认
                    symbols=[symbol],
                    source_type="debate",
                    expiry_date=(datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d'),
                    evidence={'trade_id': dt.get('trade_id'), 'verdict': dt.get('verdict')},
                ))
            elif '高信号' in reason:
                lessons.append(Lesson(
                    lesson_text=f"{symbol} 高信号但判定不合理 — 信号管线可能存在静默失败",
                    lesson_category="signal",
                    lesson_subcategory="signal_pipeline_issue",
                    impact_score=abs(dt.get('realized_pnl', 0)),
                    confidence=0.7,
                    symbols=[symbol],
                    source_type="debate",
                    expiry_date=(datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'),
                    evidence={'trade_id': dt.get('trade_id')},
                ))
        
        return lessons
    
    def _extract_from_errors(self, errors: Dict) -> List[Lesson]:
        """从错误模式提取教训"""
        lessons = []
        
        if not errors:
            return lessons
        
        # errors 结构可能是 {error_type: count} 或 {error_type: [list of errors]}
        for error_type, value in errors.items():
            if isinstance(value, list):
                count = len(value)
            elif isinstance(value, (int, float)):
                count = int(value)
            else:
                count = 1
            
            if count >= 2:
                lessons.append(Lesson(
                    lesson_text=f"重复错误: {error_type} ({count}次) — 需检查系统性问题",
                    lesson_category="risk",
                    lesson_subcategory="repeated_error",
                    impact_score=count * 50,
                    confidence=0.8,
                    source_type="auto",
                    expiry_date=(datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'),
                    evidence={'error_type': error_type, 'count': count},
                ))
        
        return lessons
    
    def _extract_from_risk(self, risk: Dict) -> List[Lesson]:
        """从风险合规提取教训"""
        lessons = []
        
        if not risk:
            return lessons
        
        risk_score = risk.get('risk_score', 10)
        if risk_score < 5:
            lessons.append(Lesson(
                lesson_text=f"风控合规评分偏低 ({risk_score}/10) — 检查仓位和止损合规性",
                lesson_category="risk",
                lesson_subcategory="low_risk_score",
                impact_score=(10 - risk_score) * 100,
                confidence=0.7,
                source_type="auto",
                expiry_date=(datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d'),
                evidence={'risk_score': risk_score},
            ))
        
        return lessons
    
    def _deduplicate(self, lessons: List[Lesson]) -> List[Lesson]:
        """去重: 相同 category + subcategory + symbol 的教训只保留 impact_score 最高的"""
        seen = {}
        for lesson in lessons:
            key = (lesson.lesson_category, lesson.lesson_subcategory, tuple(sorted(lesson.symbols)))
            if key not in seen or lesson.impact_score > seen[key].impact_score:
                seen[key] = lesson
        return list(seen.values())
    
    def _persist(self, lessons: List[Lesson]) -> int:
        """持久化到 DB (去重: 相同 text 不重复插入)"""
        if not lessons:
            return 0
        
        conn = self._get_conn()
        try:
            count = 0
            for lesson in lessons:
                # 检查是否已存在相同教训
                existing = conn.execute(
                    "SELECT id FROM lessons WHERE lesson_text = ? AND status = 'active'",
                    (lesson.lesson_text,)
                ).fetchone()
                
                if existing:
                    continue  # 跳过重复
                
                conn.execute("""
                    INSERT INTO lessons (
                        lesson_text, lesson_category, lesson_subcategory,
                        impact_score, confidence, symbols, source_type,
                        status, expiry_date, evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    lesson.lesson_text,
                    lesson.lesson_category,
                    lesson.lesson_subcategory,
                    lesson.impact_score,
                    lesson.confidence,
                    json.dumps(lesson.symbols, ensure_ascii=False),
                    lesson.source_type,
                    lesson.status,
                    lesson.expiry_date,
                    json.dumps(lesson.evidence, ensure_ascii=False),
                ))
                count += 1
            
            conn.commit()
            return count
        finally:
            conn.close()
    
    def _generate_next_day_hints(self) -> Dict:
        """生成次日交易提示"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 获取活跃教训 (未过期 + status=active)
            now = datetime.now().strftime('%Y-%m-%d')
            rows = conn.execute(
                "SELECT * FROM lessons WHERE status = 'active' AND (expiry_date = '' OR expiry_date > ?) ORDER BY impact_score DESC",
                (now,)
            ).fetchall()
            
            hints = {
                'active_lessons': [],
                'avoid_sessions': [],
                'caution_symbols': [],
                'tighten_stops_for': [],
                'lesson_count': 0,
                'top_risk': '',
            }
            
            seen_symbols = set()
            for r in rows:
                lesson = dict(r)
                lesson['symbols'] = json.loads(lesson['symbols']) if lesson['symbols'] else []
                hints['active_lessons'].append({
                    'text': lesson['lesson_text'][:100],
                    'category': lesson['lesson_category'],
                    'confidence': lesson['confidence'],
                    'expiry': lesson['expiry_date'],
                })
                
                subcat = lesson.get('lesson_subcategory', '')
                
                if subcat == 'premarket_loss' and 'premarket' not in hints['avoid_sessions']:
                    hints['avoid_sessions'].append('premarket')
                
                for sym in lesson.get('symbols', []):
                    if sym and sym not in seen_symbols:
                        seen_symbols.add(sym)
                        hints['caution_symbols'].append(sym)
                
                if subcat in ('preclose_loss', 'exit_params'):
                    if subcat not in hints['tighten_stops_for']:
                        hints['tighten_stops_for'].append(subcat)
            
            hints['lesson_count'] = len(hints['active_lessons'])
            
            # 生成顶层风险提示
            if hints['avoid_sessions']:
                hints['top_risk'] = f"应避免的交易时段: {', '.join(hints['avoid_sessions'])}"
            elif hints['caution_symbols']:
                hints['top_risk'] = f"需额外确认的标的: {', '.join(hints['caution_symbols'][:5])}"
            elif hints['lesson_count'] > 0:
                hints['top_risk'] = f"有 {hints['lesson_count']} 条活跃教训需参考"
            
            return hints
        finally:
            conn.close()
    
    def get_active_lessons(self, symbol: str = None, category: str = None) -> List[Dict]:
        """获取活跃教训 (可过滤)"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d')
            query = "SELECT * FROM lessons WHERE status = 'active' AND (expiry_date = '' OR expiry_date > ?)"
            params = [now]
            
            if category:
                query += " AND lesson_category = ?"
                params.append(category)
            
            rows = conn.execute(query, params).fetchall()
            results = []
            for r in rows:
                lesson = dict(r)
                lesson['symbols'] = json.loads(lesson['symbols']) if lesson['symbols'] else []
                lesson['evidence'] = json.loads(lesson['evidence']) if lesson['evidence'] else {}
                if symbol and symbol not in lesson['symbols']:
                    continue
                results.append(lesson)
            
            return results
        finally:
            conn.close()


# ============================================================================
#  公共接口
# ============================================================================

def accumulate_from_report(report: Dict) -> Dict:
    """从日评报告积累教训"""
    accumulator = ExperienceAccumulator()
    return accumulator.accumulate(report)
