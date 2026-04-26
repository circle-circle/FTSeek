"""
FTSeek HK 决策记录器 — 打通决策→交易→反哺全链路

职责:
1. 投票/决策实时写入 (断点1)
2. 交易结果关联 (断点3)
3. 模型/策略表现持久化 (断点2+4)
4. 回测数据供给 (断点5)

Schema:
  decisions (决策记录)
  model_votes (各模型/策略投票明细)
  trade_outcomes (交易结果关联)
  strategy_performance (策略表现累计)
  weight_adjustments (权重调整历史)
"""
import sqlite3
import json
import uuid
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ftseek.decision_recorder")

DB_PATH = "/opt/ftseek/FTSeek/data/decisions_hk.db"


class DecisionRecorder:
    """统一决策记录器 — 连接投票→交易→回测→反哺全链路"""

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        logger.info(f"[DecisionRecorder] 初始化完成: {self.db_path}")

    # ─── DB helpers ─────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                # 1. 决策记录表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS decisions (
                        decision_id     TEXT PRIMARY KEY,
                        code            TEXT NOT NULL,
                        direction       TEXT NOT NULL,        -- BUY / SELL
                        final_score     REAL NOT NULL,        -- 0-100
                        confidence      REAL NOT NULL,
                        signal_source   TEXT,                 -- fusion_v2/llm/tradingview
                        market_regime   TEXT,
                        vix             REAL,
                        hsi_trend       TEXT,
                        strategy_used   TEXT,                 -- 实际触发的策略
                        created_at      TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_code ON decisions(code)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_created ON decisions(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_dir ON decisions(direction)")

                # 2. 模型/策略投票明细表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS model_votes (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id     TEXT NOT NULL,
                        model_id        TEXT NOT NULL,        -- technical/sentiment/llm/cross_market
                        score           REAL NOT NULL,
                        direction       TEXT NOT NULL,
                        confidence      REAL NOT NULL,
                        weight          REAL NOT NULL,
                        reasoning       TEXT,
                        created_at      TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vote_decision ON model_votes(decision_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vote_model ON model_votes(model_id)")

                # 3. 交易结果关联表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS trade_outcomes (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id     TEXT NOT NULL,
                        code            TEXT NOT NULL,
                        direction       TEXT NOT NULL,
                        entry_price     REAL NOT NULL,
                        exit_price      REAL,
                        quantity        INTEGER,
                        realized_pnl    REAL DEFAULT 0,
                        pnl_pct         REAL DEFAULT 0,
                        exit_reason     TEXT,                 -- take_profit / stop_loss / time_exit
                        holding_minutes REAL,
                        outcome_correct INTEGER,              -- 1=prediction correct, 0=wrong, -1=open
                        created_at      TEXT NOT NULL,
                        updated_at      TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_decision ON trade_outcomes(decision_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_code ON trade_outcomes(code)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_correct ON trade_outcomes(outcome_correct)")

                # 4. 策略表现累计表 (持久化)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS strategy_performance (
                        strategy_id     TEXT PRIMARY KEY,
                        total_votes     INTEGER DEFAULT 0,
                        correct_votes   INTEGER DEFAULT 0,
                        wrong_votes     INTEGER DEFAULT 0,
                        avg_score       REAL DEFAULT 50,
                        total_pnl       REAL DEFAULT 0,
                        win_rate        REAL DEFAULT 0.5,
                        last_updated    TEXT NOT NULL,
                        daily_stats     TEXT                  -- JSON: {date: {votes, correct, pnl}}
                    )
                """)

                # 5. 权重调整历史表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS weight_adjustments (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        adjustment_id   TEXT NOT NULL,
                        reason          TEXT NOT NULL,
                        before_weights  TEXT NOT NULL,
                        after_weights   TEXT NOT NULL,
                        backtest_result TEXT,
                        applied         INTEGER DEFAULT 0,
                        created_at      TEXT NOT NULL
                    )
                """)

                conn.commit()
                logger.info("[DecisionRecorder] 5 张表初始化完成")
            except Exception as e:
                conn.rollback()
                logger.error(f"[DecisionRecorder] 初始化失败: {e}")
                raise
            finally:
                conn.close()

    # ─── 断点1: 决策实时写入 ─────────────────────────────────────
    def record_decision(
        self,
        code: str,
        direction: str,
        final_score: float,
        confidence: float,
        signal_source: str = "fusion",
        market_data: Dict[str, Any] = None,
        model_votes: List[Dict] = None,
        strategy_used: str = None,
    ) -> str:
        """
        记录一次完整决策 (含所有模型/策略投票明细)

        Returns:
            decision_id
        """
        decision_id = f"HK-{datetime.now().strftime('%Y%m%d%H%M%S')}-{code.replace('.', '')}-{uuid.uuid4().hex[:6]}"
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO decisions
                    (decision_id, code, direction, final_score, confidence,
                     signal_source, market_regime, vix, hsi_trend, strategy_used, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    decision_id, code, direction, final_score, confidence,
                    signal_source,
                    (market_data or {}).get("regime"),
                    (market_data or {}).get("vix"),
                    (market_data or {}).get("hsi_trend"),
                    strategy_used,
                    now,
                ))

                # 写入投票明细
                if model_votes:
                    for mv in model_votes:
                        conn.execute("""
                            INSERT INTO model_votes
                            (decision_id, model_id, score, direction, confidence, weight, reasoning, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            decision_id,
                            mv.get("model_id", "unknown"),
                            mv.get("score", 50),
                            mv.get("direction", direction),
                            mv.get("confidence", 0.5),
                            mv.get("weight", 0.0),
                            mv.get("reasoning", ""),
                            now,
                        ))

                conn.commit()
                logger.info(
                    f"[DecisionRecorder] 记录决策: {decision_id} {code} "
                    f"{direction} score={final_score:.1f}"
                )
                return decision_id

            except Exception as e:
                conn.rollback()
                logger.error(f"[DecisionRecorder] 记录决策失败: {e}")
                raise
            finally:
                conn.close()

    # ─── 断点3: 交易结果关联 ─────────────────────────────────────
    def link_trade_outcome(
        self,
        decision_id: str,
        code: str,
        direction: str,
        entry_price: float,
        quantity: int = 0,
        exit_price: float = None,
        realized_pnl: float = 0,
        exit_reason: str = None,
        holding_minutes: float = None,
    ) -> int:
        """关联交易结果到决策记录"""
        now = datetime.now().isoformat()
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if exit_price and entry_price else 0

        # Determine if prediction was correct
        outcome_correct = -1  # open
        if exit_price:
            if direction in ("BUY", "LONG"):
                outcome_correct = 1 if exit_price > entry_price else 0
            else:
                outcome_correct = 1 if exit_price < entry_price else 0

        with self._lock:
            conn = self._conn()
            try:
                cursor = conn.execute("""
                    INSERT INTO trade_outcomes
                    (decision_id, code, direction, entry_price, exit_price,
                     quantity, realized_pnl, pnl_pct, exit_reason,
                     holding_minutes, outcome_correct, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    decision_id, code, direction, entry_price, exit_price,
                    quantity, realized_pnl, pnl_pct, exit_reason,
                    holding_minutes, outcome_correct, now, now,
                ))
                conn.commit()

                # 同步更新策略表现
                if outcome_correct >= 0:
                    self._update_strategy_performance_for_decision(decision_id, outcome_correct, realized_pnl)

                logger.info(
                    f"[DecisionRecorder] 关联交易: {decision_id} {code} "
                    f"entry={entry_price} exit={exit_price} pnl={realized_pnl:.2f} correct={outcome_correct}"
                )
                return cursor.lastrowid

            except Exception as e:
                conn.rollback()
                logger.error(f"[DecisionRecorder] 关联交易失败: {e}")
                raise
            finally:
                conn.close()

    # ─── 断点2+4: 策略表现持久化 ─────────────────────────────────
    def _update_strategy_performance_for_decision(
        self, decision_id: str, outcome_correct: int, pnl: float
    ):
        """根据交易结果更新所有参与策略的表现"""
        conn = self._conn()
        try:
            votes = conn.execute(
                "SELECT model_id, score, direction FROM model_votes WHERE decision_id = ?",
                (decision_id,)
            ).fetchall()

            for vote in votes:
                model_id = vote["model_id"]
                self._update_single_strategy_performance(conn, model_id, outcome_correct, vote["score"], pnl)

            conn.commit()
        finally:
            conn.close()

    def _update_single_strategy_performance(
        self, conn, strategy_id: str, correct: int, score: float, pnl: float
    ):
        """更新单个策略的表现累计"""
        row = conn.execute(
            "SELECT * FROM strategy_performance WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()

        now = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")

        if row:
            total = row["total_votes"] + 1
            correct_votes = row["correct_votes"] + (1 if correct == 1 else 0)
            wrong_votes = row["wrong_votes"] + (1 if correct == 0 else 0)
            avg_score = (row["avg_score"] * (total - 1) + score) / total
            total_pnl = row["total_pnl"] + pnl
            win_rate = correct_votes / total if total > 0 else 0.5

            daily = json.loads(row["daily_stats"]) if row["daily_stats"] else {}
            if today not in daily:
                daily[today] = {"votes": 0, "correct": 0, "pnl": 0}
            daily[today]["votes"] += 1
            daily[today]["correct"] += (1 if correct == 1 else 0)
            daily[today]["pnl"] += pnl

            conn.execute("""
                UPDATE strategy_performance
                SET total_votes=?, correct_votes=?, wrong_votes=?, avg_score=?,
                    total_pnl=?, win_rate=?, last_updated=?, daily_stats=?
                WHERE strategy_id=?
            """, (
                total, correct_votes, wrong_votes, avg_score,
                total_pnl, win_rate, now, json.dumps(daily), strategy_id
            ))
        else:
            daily = {today: {"votes": 1, "correct": 1 if correct == 1 else 0, "pnl": pnl}}
            conn.execute("""
                INSERT INTO strategy_performance
                (strategy_id, total_votes, correct_votes, wrong_votes, avg_score,
                 total_pnl, win_rate, last_updated, daily_stats)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                strategy_id, 1,
                1 if correct == 1 else 0,
                1 if correct == 0 else 0,
                score, pnl,
                1.0 if correct == 1 else 0.0,
                now, json.dumps(daily)
            ))

    # ─── 断点5: 回测数据供给 ─────────────────────────────────────
    def get_strategy_performance(self, strategy_id: str = None) -> Dict[str, Any]:
        """获取策略表现数据 (供 AutoTuner 使用)"""
        conn = self._conn()
        try:
            if strategy_id:
                row = conn.execute(
                    "SELECT * FROM strategy_performance WHERE strategy_id = ?", (strategy_id,)
                ).fetchone()
                if row:
                    result = dict(row)
                    if result.get("daily_stats"):
                        result["daily_stats"] = json.loads(result["daily_stats"])
                    return result
                return {}
            else:
                rows = conn.execute("SELECT * FROM strategy_performance").fetchall()
                result = {}
                for row in rows:
                    d = dict(row)
                    if d.get("daily_stats"):
                        d["daily_stats"] = json.loads(d["daily_stats"])
                    result[d["strategy_id"]] = d
                return result
        finally:
            conn.close()

    def get_decision_history(
        self, code: str = None, start: str = None, end: str = None, limit: int = 100
    ) -> List[Dict]:
        """获取决策历史 (供复盘和回测使用)"""
        conn = self._conn()
        try:
            query = """
                SELECT d.*, GROUP_CONCAT(m.model_id || ':' || m.score || ':' || m.direction) as model_details
                FROM decisions d
                LEFT JOIN model_votes m ON d.decision_id = m.decision_id
                WHERE 1=1
            """
            params = []
            if code:
                query += " AND d.code = ?"
                params.append(code)
            if start:
                query += " AND d.created_at >= ?"
                params.append(start)
            if end:
                query += " AND d.created_at <= ?"
                params.append(end)
            query += " GROUP BY d.decision_id ORDER BY d.created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_trade_outcomes(self, decision_id: str = None, code: str = None) -> List[Dict]:
        """获取交易结果"""
        conn = self._conn()
        try:
            query = "SELECT * FROM trade_outcomes WHERE 1=1"
            params = []
            if decision_id:
                query += " AND decision_id = ?"
                params.append(decision_id)
            if code:
                query += " AND code = ?"
                params.append(code)
            query += " ORDER BY created_at DESC"

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_weight_adjustment_history(self, limit: int = 20) -> List[Dict]:
        """获取权重调整历史"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM weight_adjustments ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["before_weights"] = json.loads(d["before_weights"])
                d["after_weights"] = json.loads(d["after_weights"])
                if d.get("backtest_result"):
                    d["backtest_result"] = json.loads(d["backtest_result"])
                result.append(d)
            return result
        finally:
            conn.close()

    def record_weight_adjustment(
        self,
        reason: str,
        before_weights: Dict,
        after_weights: Dict,
        backtest_result: Dict = None,
        applied: bool = False,
    ) -> str:
        """记录权重调整"""
        adj_id = f"HK-WADJ-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO weight_adjustments
                    (adjustment_id, reason, before_weights, after_weights,
                     backtest_result, applied, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    adj_id, reason,
                    json.dumps(before_weights),
                    json.dumps(after_weights),
                    json.dumps(backtest_result) if backtest_result else None,
                    1 if applied else 0,
                    now,
                ))
                conn.commit()
                logger.info(f"[DecisionRecorder] 记录权重调整: {adj_id} ({reason})")
                return adj_id
            except Exception as e:
                conn.rollback()
                logger.error(f"[DecisionRecorder] 记录权重调整失败: {e}")
                raise
            finally:
                conn.close()


# ─── Singleton ──────────────────────────────────────────────────
_instance = None
_lock = threading.Lock()


def get_decision_recorder() -> DecisionRecorder:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = DecisionRecorder()
    return _instance
