"""
Playbook Database Layer — 表创建 + CRUD 操作

Tables (spec 6.1):
  1. playbook_instructions — 交易指令 (Layer 1 产出)
  2. playbook_factor_stats — 因子统计 (双层分析)
  3. playbook_patterns — 精确组合 (高频)
  4. playbook_user_feedback — 用户反馈
  5. playbook_regime_log — 市场环境日志

Usage:
  from app.playbook.db import PlaybookDB

  db = PlaybookDB()
  db.ensure_tables()  # 首次调用创建表

  # 写入指令
  db.save_instruction(instruction_dict)

  # 读取今日指令
  instructions = db.get_instructions_by_date("2026-06-18")
"""
import json
import logging
import sqlite3
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.storage.db_pool import get_db

logger = logging.getLogger(__name__)


class PlaybookDB:
    """Playbook 数据库操作"""

    def __init__(self, db_name: str = "ftseek_us", conn: sqlite3.Connection = None):
        """
        Args:
            db_name: 数据库池名称 (生产用)
            conn: 直接传入连接 (测试用, 跳过连接池)
        """
        self.db_name = db_name
        self._direct_conn = conn
        self._tables_created = False

    def _conn(self):
        """获取连接"""
        if self._direct_conn is not None:
            return self._direct_conn
        return get_db(self.db_name).connection()

    def ensure_tables(self):
        """创建所有 playbook 表 (IF NOT EXISTS)"""
        conn = self._conn()
        try:
            conn.executescript(_SCHEMA_SQL)
            self._tables_created = True
            logger.info("Playbook tables ensured")
        except Exception as e:
            logger.error(f"Failed to create playbook tables: {e}")
            raise

    # ========== playbook_instructions ==========

    def save_instruction(self, inst: Dict[str, Any]) -> int:
        """
        写入/更新一条交易指令 (UPSERT by date+symbol)

        Args:
            inst: 指令 dict, 必须包含 date, symbol, direction

        Returns:
            row id
        """
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT INTO playbook_instructions (
                date, symbol, direction, skip_reason,
                entry_price, stop_loss, take_profit,
                position_size, risk_amount, risk_reward, confidence,
                conditions_json, thesis, sector, risks_json,
                market_regime, vix_at_generation, spy_trend_at_generation,
                data_quality_json, ok_dimensions,
                matched_patterns_json, pattern_verdict,
                raw_instruction_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, symbol) DO UPDATE SET
                direction=excluded.direction,
                skip_reason=excluded.skip_reason,
                entry_price=excluded.entry_price,
                stop_loss=excluded.stop_loss,
                take_profit=excluded.take_profit,
                position_size=excluded.position_size,
                risk_amount=excluded.risk_amount,
                risk_reward=excluded.risk_reward,
                confidence=excluded.confidence,
                conditions_json=excluded.conditions_json,
                thesis=excluded.thesis,
                sector=excluded.sector,
                risks_json=excluded.risks_json,
                market_regime=excluded.market_regime,
                vix_at_generation=excluded.vix_at_generation,
                data_quality_json=excluded.data_quality_json,
                ok_dimensions=excluded.ok_dimensions,
                raw_instruction_json=excluded.raw_instruction_json,
                updated_at=excluded.updated_at
        """, (
            inst.get("date"),
            inst.get("symbol"),
            inst.get("direction"),
            inst.get("skip_reason"),
            inst.get("entry_price"),
            inst.get("stop_loss"),
            inst.get("take_profit"),
            inst.get("position_size"),
            inst.get("risk_amount"),
            inst.get("risk_reward"),
            inst.get("confidence"),
            _to_json(inst.get("conditions")),
            inst.get("thesis"),
            inst.get("sector"),
            _to_json(inst.get("risks")),
            inst.get("market_regime"),
            inst.get("vix_at_generation"),
            inst.get("spy_trend_at_generation"),
            _to_json(inst.get("data_quality")),
            inst.get("ok_dimensions"),
            _to_json(inst.get("matched_patterns")),
            inst.get("pattern_verdict"),
            _to_json(inst.get("raw_instruction")),
            inst.get("created_at", now),
            now,
        ))
        conn.commit()
        # UPSERT: last_insert_rowid() 在 UPDATE 时返回 0, 需要显式查询
        row = conn.execute(
            "SELECT id FROM playbook_instructions WHERE date = ? AND symbol = ?",
            (inst.get("date"), inst.get("symbol"))
        ).fetchone()
        return row[0] if row else 0

    def get_instructions_by_date(self, date: str) -> List[Dict]:
        """获取某日所有指令"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_instructions WHERE date = ? ORDER BY id",
            (date,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_instruction(self, date: str, symbol: str) -> Optional[Dict]:
        """获取某日某标的指令"""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM playbook_instructions WHERE date = ? AND symbol = ?",
            (date, symbol)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def update_instruction_tracking(self, instruction_id: int, updates: Dict[str, Any]):
        """更新指令的追踪字段 (Layer 3 盘后填充)"""
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        set_clauses = []
        values = []
        for key, value in updates.items():
            if key in _TRACKING_FIELDS:
                set_clauses.append(f"{key} = ?")
                values.append(value)

        if not set_clauses:
            return

        set_clauses.append("updated_at = ?")
        values.append(now)
        values.append(instruction_id)

        conn.execute(
            f"UPDATE playbook_instructions SET {', '.join(set_clauses)} WHERE id = ?",
            values
        )
        conn.commit()

    def get_instructions_with_outcome(self, days_back: int = 90) -> List[Dict]:
        """获取有结果的指令 (用于因子分析)"""
        conn = self._conn()
        rows = conn.execute("""
            SELECT * FROM playbook_instructions
            WHERE outcome IS NOT NULL
              AND outcome != 'holding'
              AND direction != 'SKIP'
            ORDER BY date DESC
            LIMIT ?
        """, (days_back * 10,)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_instructions(self, limit: int = 50) -> List[Dict]:
        """获取最近的指令"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_instructions ORDER BY date DESC, id LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== playbook_factor_stats ==========

    def upsert_factor_stat(self, stat: Dict[str, Any]):
        """写入/更新因子统计"""
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT INTO playbook_factor_stats (
                factor_key, analysis_level, co_factors_json,
                total_trades, wins, losses, win_rate,
                avg_pnl, avg_pnl_pct, total_pnl, profit_factor,
                max_win, max_loss,
                bayesian_win_rate, bayesian_lower, bayesian_upper,
                effective_sample_size,
                verdict, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(factor_key, analysis_level) DO UPDATE SET
                total_trades=excluded.total_trades,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                avg_pnl=excluded.avg_pnl,
                avg_pnl_pct=excluded.avg_pnl_pct,
                total_pnl=excluded.total_pnl,
                profit_factor=excluded.profit_factor,
                max_win=excluded.max_win,
                max_loss=excluded.max_loss,
                bayesian_win_rate=excluded.bayesian_win_rate,
                bayesian_lower=excluded.bayesian_lower,
                bayesian_upper=excluded.bayesian_upper,
                effective_sample_size=excluded.effective_sample_size,
                verdict=excluded.verdict,
                last_updated=excluded.last_updated
        """, (
            stat.get("factor_key"),
            stat.get("analysis_level"),
            _to_json(stat.get("co_factors")),
            stat.get("total_trades", 0),
            stat.get("wins", 0),
            stat.get("losses", 0),
            stat.get("win_rate", 0),
            stat.get("avg_pnl", 0),
            stat.get("avg_pnl_pct", 0),
            stat.get("total_pnl", 0),
            stat.get("profit_factor", 0),
            stat.get("max_win", 0),
            stat.get("max_loss", 0),
            stat.get("bayesian_win_rate"),
            stat.get("bayesian_lower"),
            stat.get("bayesian_upper"),
            stat.get("effective_sample_size"),
            stat.get("verdict", "observing"),
            now,
        ))
        conn.commit()

    def get_factor_stats(self, level: str = None, verdict: str = None) -> List[Dict]:
        """获取因子统计"""
        conn = self._conn()
        query = "SELECT * FROM playbook_factor_stats WHERE 1=1"
        params = []

        if level:
            query += " AND analysis_level = ?"
            params.append(level)
        if verdict:
            query += " AND verdict = ?"
            params.append(verdict)

        query += " ORDER BY total_trades DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== playbook_patterns ==========

    def upsert_pattern(self, pattern: Dict[str, Any]):
        """写入/更新 pattern"""
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT INTO playbook_patterns (
                pattern_key, conditions_json,
                total_trades, wins, losses, win_rate,
                avg_pnl, avg_pnl_pct, max_win, max_loss, profit_factor,
                bayesian_win_rate, bayesian_lower, bayesian_upper,
                status, first_seen, last_seen, last_updated, weekly_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pattern_key) DO UPDATE SET
                total_trades=excluded.total_trades,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                avg_pnl=excluded.avg_pnl,
                avg_pnl_pct=excluded.avg_pnl_pct,
                max_win=excluded.max_win,
                max_loss=excluded.max_loss,
                profit_factor=excluded.profit_factor,
                bayesian_win_rate=excluded.bayesian_win_rate,
                bayesian_lower=excluded.bayesian_lower,
                bayesian_upper=excluded.bayesian_upper,
                status=excluded.status,
                last_seen=excluded.last_seen,
                last_updated=excluded.last_updated,
                weekly_notes=excluded.weekly_notes
        """, (
            pattern.get("pattern_key"),
            _to_json(pattern.get("conditions")),
            pattern.get("total_trades", 0),
            pattern.get("wins", 0),
            pattern.get("losses", 0),
            pattern.get("win_rate", 0),
            pattern.get("avg_pnl", 0),
            pattern.get("avg_pnl_pct", 0),
            pattern.get("max_win", 0),
            pattern.get("max_loss", 0),
            pattern.get("profit_factor", 0),
            pattern.get("bayesian_win_rate"),
            pattern.get("bayesian_lower"),
            pattern.get("bayesian_upper"),
            pattern.get("status", "observing"),
            pattern.get("first_seen", now),
            pattern.get("last_seen", now),
            now,
            pattern.get("weekly_notes"),
        ))
        conn.commit()

    def get_patterns(self, status: str = None) -> List[Dict]:
        """获取 patterns"""
        conn = self._conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM playbook_patterns WHERE status = ? ORDER BY total_trades DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM playbook_patterns ORDER BY total_trades DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== playbook_user_feedback ==========

    def save_user_feedback(self, feedback: Dict[str, Any]) -> int:
        """保存用户反馈"""
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        cursor = conn.execute("""
            INSERT INTO playbook_user_feedback (
                instruction_id, action, skip_reason,
                modified_field, original_value, modified_value,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            feedback.get("instruction_id"),
            feedback.get("action"),
            feedback.get("skip_reason"),
            feedback.get("modified_field"),
            feedback.get("original_value"),
            feedback.get("modified_value"),
            now,
        ))
        conn.commit()
        return cursor.lastrowid

    def get_user_feedback(self, instruction_id: int) -> List[Dict]:
        """获取某指令的用户反馈"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_user_feedback WHERE instruction_id = ?",
            (instruction_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_feedback(self, days: int = 7) -> List[Dict]:
        """获取最近 N 天的所有用户反馈 (Phase 1: 反馈学习)"""
        conn = self._conn()
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM playbook_user_feedback WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,)
        ).fetchall()
        return [d for r in rows if (d := _row_to_dict(r)) is not None]

    def get_instruction_by_id(self, instruction_id: int) -> Optional[Dict]:
        """通过 ID 获取单条指令"""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM playbook_instructions WHERE id = ?",
            (instruction_id,)
        ).fetchone()
        return _row_to_dict(row)

    # ========== playbook_regime_log ==========

    def save_regime_log(self, regime_data: Dict[str, Any]):
        """保存市场环境日志 (每日一条, UPSERT by date)"""
        conn = self._conn()
        now = datetime.utcnow().isoformat()

        conn.execute("""
            INSERT INTO playbook_regime_log (
                date, regime, vix, spy_price, spy_ma20, spy_ma50,
                score, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                regime=excluded.regime,
                vix=excluded.vix,
                spy_price=excluded.spy_price,
                spy_ma20=excluded.spy_ma20,
                spy_ma50=excluded.spy_ma50,
                score=excluded.score,
                notes=excluded.notes
        """, (
            regime_data.get("date"),
            regime_data.get("regime"),
            regime_data.get("vix"),
            regime_data.get("spy_price"),
            regime_data.get("spy_ma20"),
            regime_data.get("spy_ma50"),
            regime_data.get("score"),
            regime_data.get("notes"),
            now,
        ))
        conn.commit()

    def get_regime_log(self, days: int = 30) -> List[Dict]:
        """获取近期 regime 日志"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM playbook_regime_log ORDER BY date DESC LIMIT ?",
            (days,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ========== 汇总统计 ==========

    def get_summary_stats(self, days: int = 30) -> Dict[str, Any]:
        """获取汇总统计"""
        conn = self._conn()

        # 总指令数
        total = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE direction != 'SKIP'"
        ).fetchone()[0]

        # 执行了的
        executed = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE executed = 1"
        ).fetchone()[0]

        # 胜率
        wins = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE outcome = 'win'"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE outcome = 'loss'"
        ).fetchone()[0]

        # 总盈亏
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM playbook_instructions WHERE executed = 1"
        ).fetchone()[0]

        decided = wins + losses
        win_rate = (wins / decided * 100) if decided > 0 else 0

        return {
            "total_instructions": total,
            "executed": executed,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
        }


# ========== 辅助函数 ==========

_TRACKING_FIELDS = {
    "triggered", "executed", "execution_price", "execution_time",
    "exit_price", "exit_time", "exit_reason", "holding_period_hours",
    "pnl", "pnl_pct",
    "max_favorable_excursion", "max_adverse_excursion", "mfe_analysis",
    "outcome", "shadow_pnl", "lesson",
    # Phase 1: IC 追踪
    "fwd_return_t1", "fwd_return_t3", "fwd_return_t5",
}


def _to_json(value) -> Optional[str]:
    """安全地将值转为 JSON 字符串"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _row_to_dict(row) -> Optional[Dict]:
    """将 sqlite3.Row 转为 dict, 自动解析 JSON 字段"""
    if row is None:
        return None
    d = dict(row)
    # 自动解析 JSON 字段
    for key in ("conditions_json", "risks_json", "data_quality_json",
                "matched_patterns_json", "raw_instruction_json",
                "co_factors_json"):
        if key in d and d[key]:
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ========== Schema SQL ==========

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS playbook_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    skip_reason TEXT,

    -- 执行参数
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    position_size INTEGER,
    risk_amount REAL,
    risk_reward REAL,
    confidence INTEGER,

    -- 选股条件
    conditions_json TEXT,
    thesis TEXT,
    sector TEXT,
    risks_json TEXT,

    -- 市场环境
    market_regime TEXT,
    vix_at_generation REAL,
    spy_trend_at_generation TEXT,

    -- 数据质量
    data_quality_json TEXT,
    ok_dimensions INTEGER,

    -- Pattern 匹配
    matched_patterns_json TEXT,
    pattern_verdict TEXT,

    -- 追踪
    triggered INTEGER DEFAULT 0,
    executed INTEGER DEFAULT 0,
    execution_price REAL,
    execution_time TEXT,
    exit_price REAL,
    exit_time TEXT,
    exit_reason TEXT,
    holding_period_hours REAL,
    pnl REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,

    -- MFE/MAE
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    mfe_analysis TEXT,

    outcome TEXT,
    shadow_pnl REAL,

    -- IC 追踪 (Phase 1: 2026-06-19)
    fwd_return_t1 REAL,
    fwd_return_t3 REAL,
    fwd_return_t5 REAL,

    -- 复盘
    lesson TEXT,

    -- LLM 原始输出
    raw_instruction_json TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT,

    UNIQUE(date, symbol)
);

CREATE TABLE IF NOT EXISTS playbook_factor_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_key TEXT NOT NULL,
    analysis_level TEXT NOT NULL,
    co_factors_json TEXT,

    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,

    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,
    effective_sample_size REAL,

    verdict TEXT DEFAULT 'observing',
    last_updated TEXT,

    UNIQUE(factor_key, analysis_level)
);

CREATE TABLE IF NOT EXISTS playbook_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key TEXT NOT NULL UNIQUE,
    conditions_json TEXT NOT NULL,

    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,

    bayesian_win_rate REAL,
    bayesian_lower REAL,
    bayesian_upper REAL,

    status TEXT DEFAULT 'observing',
    first_seen TEXT,
    last_seen TEXT,
    last_updated TEXT,
    weekly_notes TEXT
);

CREATE TABLE IF NOT EXISTS playbook_user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id INTEGER NOT NULL REFERENCES playbook_instructions(id),
    action TEXT NOT NULL,
    skip_reason TEXT,
    modified_field TEXT,
    original_value TEXT,
    modified_value TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playbook_regime_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    regime TEXT NOT NULL,
    vix REAL,
    spy_price REAL,
    spy_ma20 REAL,
    spy_ma50 REAL,
    score INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playbook_instructions_date
    ON playbook_instructions(date);

CREATE INDEX IF NOT EXISTS idx_playbook_instructions_outcome
    ON playbook_instructions(outcome);

CREATE INDEX IF NOT EXISTS idx_playbook_factor_stats_level
    ON playbook_factor_stats(analysis_level);

CREATE INDEX IF NOT EXISTS idx_playbook_patterns_status
    ON playbook_patterns(status);
"""
