"""
FTSeek-US 决策记录器 — 打通 5 个断点的核心纽带

职责:
1. 投票决策实时写入 (断点1)
2. 交易结果关联 (断点3)
3. 模型表现追踪持久化 (断点2+4)
4. 回测数据供给 (断点5)
5. 自动调参数据源

Schema:
  decisions (投票决策记录)
  model_votes (各模型独立投票)
  trade_outcomes (交易结果关联)
  model_performance (模型表现累计)
"""
import sqlite3
import json
import logging
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.core.paths import get_decisions_db


def uuid4():
    return uuid.uuid4().hex

logger = logging.getLogger("ftseek_us.decision_recorder")

DB_PATH = str(get_decisions_db())


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
                        symbol          TEXT NOT NULL,
                        direction       TEXT NOT NULL,        -- LONG / SHORT
                        final_strength  INTEGER NOT NULL,     -- 0-100
                        consensus_level REAL NOT NULL,
                        std_dev         REAL NOT NULL,
                        llm_weight      REAL,                 -- LLM 动态权重
                        llm_strength    INTEGER,              -- LLM 外部投票强度
                        auto_debate     INTEGER DEFAULT 0,    -- 是否触发自动辩论
                        market_regime   TEXT,
                        vix             REAL,
                        created_at      TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_symbol ON decisions(symbol)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_created ON decisions(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_dir ON decisions(direction)")

                # 2. 模型投票明细表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS model_votes (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id     TEXT NOT NULL,
                        model_id        TEXT NOT NULL,        -- qwen/minimax/glm/llm_strategy
                        model_role      TEXT,                 -- QWEN/MINIMAX/GLM
                        strength        INTEGER NOT NULL,
                        direction       TEXT NOT NULL,
                        confidence      REAL NOT NULL,
                        weight          REAL NOT NULL,        -- 实际权重
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
                        symbol          TEXT NOT NULL,
                        direction       TEXT NOT NULL,
                        entry_price     REAL NOT NULL,
                        exit_price      REAL,
                        quantity        INTEGER,
                        realized_pnl    REAL DEFAULT 0,
                        pnl_pct         REAL DEFAULT 0,
                        exit_reason     TEXT,                 -- take_profit / stop_loss / time_exit / manual
                        holding_hours   REAL,
                        outcome_correct INTEGER,              -- 1=prediction correct, 0=wrong, -1=open
                        created_at      TEXT NOT NULL,
                        updated_at      TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_decision ON trade_outcomes(decision_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_symbol ON trade_outcomes(symbol)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome_correct ON trade_outcomes(outcome_correct)")

                # G13: session-strategy alignment columns (added post-init)
                try:
                    conn.execute("ALTER TABLE trade_outcomes ADD COLUMN entry_session TEXT")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE trade_outcomes ADD COLUMN strategy_type TEXT")
                except sqlite3.OperationalError:
                    pass

                # 4. 模型表现累计表 (持久化，不随重启丢失)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS model_performance (
                        model_id        TEXT PRIMARY KEY,
                        total_votes     INTEGER DEFAULT 0,
                        correct_votes   INTEGER DEFAULT 0,
                        wrong_votes     INTEGER DEFAULT 0,
                        avg_strength    REAL DEFAULT 50,
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
                        reason          TEXT NOT NULL,        -- weekly_review / monthly_tune / manual
                        before_weights  TEXT NOT NULL,        -- JSON
                        after_weights   TEXT NOT NULL,        -- JSON
                        backtest_result TEXT,                 -- 回测验证结果
                        applied         INTEGER DEFAULT 0,    -- 是否已应用
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

    # ─── 断点1: 投票决策实时写入 ───────────────────────────────
    def record_decision(
        self,
        symbol: str,
        voting_result: Dict[str, Any],
        market_data: Optional[Dict[str, Any]] = None,
        voting_llm: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        记录一次完整投票决策 (含所有模型明细)

        Args:
            symbol: US.AAPL
            voting_result: aggregator.aggregate() 返回结果
            market_data: 市场状态快照
            voting_llm: LLM 外部投票信息

        Returns:
            decision_id
        """
        # P1 修复 (2026-05-31): 统一用 ET 时间存储, 与 DB SSOT 对齐
        from app.utils.timezone import now_et
        _now = now_et()
        decision_id = f"DEC-{_now.strftime('%Y%m%d%H%M%S')}-{symbol.split('.')[-1]}-{uuid4()[:6]}"
        now = _now.isoformat()

        with self._lock:
            conn = self._conn()
            try:
                # 写入决策主表
                conn.execute("""
                    INSERT INTO decisions
                    (decision_id, symbol, direction, final_strength, consensus_level,
                     std_dev, llm_weight, llm_strength, auto_debate,
                     market_regime, vix, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    decision_id,
                    symbol,
                    voting_result.get("direction", "NEUTRAL"),
                    voting_result.get("final_strength", 50),
                    voting_result.get("consensus_level", 0),
                    voting_result.get("std_dev", 0),
                    voting_llm.get("weight") if voting_llm else None,
                    voting_llm.get("strength") if voting_llm else None,
                    1 if voting_result.get("auto_debate_triggered", False) else 0,
                    market_data.get("regime") if market_data else None,
                    market_data.get("vix") if market_data else None,
                    now,
                ))

                # 写入模型投票明细
                model_scores_list = voting_result.get("model_scores", [])

                # ─── 诊断日志 (2026-06-02: 定位 model_votes=0 的根因) ───
                if not model_scores_list:
                    logger.info(
                        f"[DecisionRecorder] ⚠️ model_scores 为空, 无 model_votes 写入: "
                        f"decision_id={decision_id} symbol={symbol}"
                    )
                else:
                    logger.info(
                        f"[DecisionRecorder] model_scores: {len(model_scores_list)} 条, "
                        f"ids={[ms.get('model_id', '?') for ms in model_scores_list]}"
                    )

                _votes_inserted = 0
                for ms in model_scores_list:
                    model_id = ms.get("model_id", ms.get("model", "unknown"))
                    direction = ms.get("direction", "NEUTRAL")
                    # Handle int direction (1/-1 from LLM)
                    if isinstance(direction, int):
                        direction = "LONG" if direction > 0 else "SHORT" if direction < 0 else "NEUTRAL"

                    # Determine model role
                    role_map = {"qwen": "QWEN", "minimax": "MINIMAX", "llm_strategy": "LLM_STRATEGY"}
                    model_role = role_map.get(model_id.lower() if model_id else "unknown", "UNKNOWN")

                    conn.execute("""
                        INSERT INTO model_votes
                        (decision_id, model_id, model_role, strength, direction,
                         confidence, weight, reasoning, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        decision_id,
                        model_id,
                        model_role,
                        ms.get("strength", 50),
                        direction,
                        ms.get("confidence", 0.5),
                        ms.get("weight", 0.0),
                        ms.get("reasoning", ""),
                        now,
                    ))
                    _votes_inserted += 1

                if _votes_inserted > 0:
                    logger.info(f"[DecisionRecorder] ✅ 写入 {_votes_inserted} 条 model_votes: {decision_id}")

                conn.commit()
                logger.info(
                    f"[DecisionRecorder] 记录决策: {decision_id} {symbol} "
                    f"{voting_result.get('direction')} strength={voting_result.get('final_strength')}"
                )

                # [SignalIC Phase 1] 记录信号快照到 signal_snapshots.db
                # 非阻塞: 失败不影响主流程, 仅 debug 级别日志
                try:
                    from app.analysis.signal_ic_tracker import record_signal_snapshot
                    record_signal_snapshot(
                        decision_id=decision_id,
                        symbol=symbol,
                        direction=voting_result.get("direction", "NEUTRAL"),
                        strength=voting_result.get("final_strength", 50),
                        confidence=voting_result.get("consensus_level", 0),
                        consensus_level=voting_result.get("consensus_level", 0),
                        strategy_id=voting_result.get("strategy_id"),
                        market_context=market_data,  # P1 (2026-07-11): 主通道 regime/vix 市场上下文持久化
                        created_at=now,
                    )
                except Exception as _ic_e:
                    logger.debug(f"[DecisionRecorder] SignalIC 快照记录失败 (不影响主流程): {_ic_e}")

                return decision_id

            except Exception as e:
                conn.rollback()
                logger.error(f"[DecisionRecorder] 记录决策失败: {e}")
                raise
            finally:
                conn.close()

    # ─── 断点3: 交易结果关联 ───────────────────────────────────
    def link_trade_outcome(
        self,
        decision_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: int = 0,
        exit_price: float = None,
        realized_pnl: float = 0,
        exit_reason: str = None,
        holding_hours: float = None,
        entry_session: str = None,
        strategy_type: str = None,
    ) -> int:
        """关联交易结果到决策记录

        P2 G13: entry_session + strategy_type 用于时段-策略对齐学习
        """
        now = datetime.now().isoformat()
        # DEB-20260721 B-1: 增加方向感知 (原式 SHORT 符号反); 与下方 outcome_correct
        # 的 LONG/非LONG 二分保持一致. 注意: 本表 pnl_pct 约定为百分比 (0-100 标尺),
        # 与 trade_records.pnl_pct (比例 0-1) 不同, 跨表使用需换算.
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if exit_price and entry_price else 0
        if pnl_pct and direction != "LONG":
            pnl_pct = -pnl_pct

        # Determine if prediction was correct
        outcome_correct = -1  # open
        if exit_price:
            if direction == "LONG":
                outcome_correct = 1 if exit_price > entry_price else 0
            else:
                outcome_correct = 1 if exit_price < entry_price else 0

        with self._lock:
            conn = self._conn()
            try:
                # P0-6 修复 (2026-05-03): UPSERT 模式 — 避免同一 decision_id 开仓/平仓各 INSERT 一条
                # 先检查是否已存在
                existing = conn.execute(
                    "SELECT id FROM trade_outcomes WHERE decision_id = ? AND symbol = ?",
                    (decision_id, symbol)
                ).fetchone()
                
                if existing:
                    # 更新已有记录
                    conn.execute("""
                        UPDATE trade_outcomes SET
                            direction = ?, entry_price = ?, exit_price = ?,
                            quantity = ?, realized_pnl = ?, pnl_pct = ?,
                            exit_reason = ?, holding_hours = ?, outcome_correct = ?,
                            entry_session = ?, strategy_type = ?, updated_at = ?
                        WHERE decision_id = ? AND symbol = ?
                    """, (
                        direction, entry_price, exit_price,
                        quantity, realized_pnl, pnl_pct,
                        exit_reason, holding_hours, outcome_correct,
                        entry_session, strategy_type, now,
                        decision_id, symbol,
                    ))
                    row_id = existing[0]
                else:
                    # 插入新记录
                    cursor = conn.execute("""
                        INSERT INTO trade_outcomes
                        (decision_id, symbol, direction, entry_price, exit_price,
                         quantity, realized_pnl, pnl_pct, exit_reason,
                         holding_hours, outcome_correct, entry_session, strategy_type,
                         created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        decision_id, symbol, direction, entry_price, exit_price,
                        quantity, realized_pnl, pnl_pct, exit_reason,
                        holding_hours, outcome_correct, entry_session, strategy_type,
                        now, now,
                    ))
                    row_id = cursor.lastrowid
                
                conn.commit()

                # 同步更新模型表现
                if outcome_correct >= 0:
                    self._update_model_performance_for_decision(decision_id, outcome_correct, realized_pnl)

                logger.info(
                    f"[DecisionRecorder] 关联交易: {decision_id} {symbol} "
                    f"entry={entry_price} exit={exit_price} pnl={realized_pnl:.2f} correct={outcome_correct}"
                )
                return row_id

            except Exception as e:
                conn.rollback()
                logger.error(f"[DecisionRecorder] 关联交易失败: {e}")
                raise
            finally:
                conn.close()

    # ─── 断点2+4: 模型表现持久化 ───────────────────────────────
    def _update_model_performance_for_decision(
        self, decision_id: str, outcome_correct: int, pnl: float
    ):
        """根据交易结果更新所有参与模型的表现"""
        conn = self._conn()
        try:
            # 获取该决策的所有模型投票
            votes = conn.execute(
                "SELECT model_id, strength, direction FROM model_votes WHERE decision_id = ?",
                (decision_id,)
            ).fetchall()

            for vote in votes:
                model_id = vote["model_id"]
                self._update_single_model_performance(conn, model_id, outcome_correct, vote["strength"], pnl)

            conn.commit()
        finally:
            conn.close()

    def _update_single_model_performance(
        self, conn, model_id: str, correct: int, strength: int, pnl: float
    ):
        """更新单个模型的表现累计"""
        row = conn.execute(
            "SELECT * FROM model_performance WHERE model_id = ?", (model_id,)
        ).fetchone()

        now = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")

        if row:
            total = row["total_votes"] + 1
            correct_votes = row["correct_votes"] + (1 if correct == 1 else 0)
            wrong_votes = row["wrong_votes"] + (1 if correct == 0 else 0)
            avg_str = (row["avg_strength"] * (total - 1) + strength) / total
            total_pnl = row["total_pnl"] + pnl
            win_rate = correct_votes / total if total > 0 else 0.5

            # Update daily stats
            daily = json.loads(row["daily_stats"]) if row["daily_stats"] else {}
            if today not in daily:
                daily[today] = {"votes": 0, "correct": 0, "pnl": 0}
            daily[today]["votes"] += 1
            daily[today]["correct"] += (1 if correct == 1 else 0)
            daily[today]["pnl"] += pnl

            conn.execute("""
                UPDATE model_performance
                SET total_votes=?, correct_votes=?, wrong_votes=?, avg_strength=?,
                    total_pnl=?, win_rate=?, last_updated=?, daily_stats=?
                WHERE model_id=?
            """, (
                total, correct_votes, wrong_votes, avg_str,
                total_pnl, win_rate, now, json.dumps(daily), model_id
            ))
        else:
            daily = {today: {"votes": 1, "correct": 1 if correct == 1 else 0, "pnl": pnl}}
            conn.execute("""
                INSERT INTO model_performance
                (model_id, total_votes, correct_votes, wrong_votes, avg_strength,
                 total_pnl, win_rate, last_updated, daily_stats)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                model_id, 1,
                1 if correct == 1 else 0,
                1 if correct == 0 else 0,
                strength, pnl,
                1.0 if correct == 1 else 0.0,
                now, json.dumps(daily)
            ))

    # ─── 断点5: 回测数据供给 ───────────────────────────────────
    def get_model_performance(self, model_id: str = None) -> Dict[str, Any]:
        """获取模型表现数据 (供 AutoTuner 使用)"""
        conn = self._conn()
        try:
            if model_id:
                row = conn.execute(
                    "SELECT * FROM model_performance WHERE model_id = ?", (model_id,)
                ).fetchone()
                if row:
                    result = dict(row)
                    if result.get("daily_stats"):
                        result["daily_stats"] = json.loads(result["daily_stats"])
                    return result
                return {}
            else:
                rows = conn.execute("SELECT * FROM model_performance").fetchall()
                result = {}
                for row in rows:
                    d = dict(row)
                    if d.get("daily_stats"):
                        d["daily_stats"] = json.loads(d["daily_stats"])
                    result[d["model_id"]] = d
                return result
        finally:
            conn.close()

    def get_decision_history(
        self, symbol: str = None, start: str = None, end: str = None, limit: int = 100
    ) -> List[Dict]:
        """获取决策历史 (供复盘和回测使用)"""
        conn = self._conn()
        try:
            query = """
                SELECT d.*, GROUP_CONCAT(m.model_id || ':' || m.strength || ':' || m.direction) as model_details
                FROM decisions d
                LEFT JOIN model_votes m ON d.decision_id = m.decision_id
                WHERE 1=1
            """
            params = []
            if symbol:
                query += " AND d.symbol = ?"
                params.append(symbol)
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

    def get_trade_outcomes(self, decision_id: str = None, symbol: str = None) -> List[Dict]:
        """获取交易结果"""
        conn = self._conn()
        try:
            query = "SELECT * FROM trade_outcomes WHERE 1=1"
            params = []
            if decision_id:
                query += " AND decision_id = ?"
                params.append(decision_id)
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
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
        adj_id = f"WADJ-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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

    # ─── P2 G10: 因子权重学习 ───────────────────────────────────

    def learn_factor_weights(self, days: int = 30, min_trades: int = 10) -> Dict[str, Any]:
        """从 trade_outcomes 数据学习各技术因子的有效权重

        关联 decisions.model_votes → trade_outcomes,
        计算每个 tech_* 因子投票方向与交易结果的一致性。
        胜率高的因子 → 提升权重; 胜率低的 → 降低权重。

        Returns:
            {
                "learned_weights": {"trend": 0.30, "momentum": 0.20, ...},
                "factor_accuracy": {"trend": 0.62, "momentum": 0.55, ...},
                "total_trades": 25,
                "confidence": "high" | "medium" | "low",
            }
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._conn()
        try:
            # 获取已平仓交易的 decision_id 列表
            outcome_rows = conn.execute(
                "SELECT decision_id, direction, outcome_correct "
                "FROM trade_outcomes "
                "WHERE outcome_correct IN (0, 1) AND created_at >= ?",
                (cutoff,)
            ).fetchall()

            if len(outcome_rows) < min_trades:
                return {
                    "learned_weights": {},
                    "factor_accuracy": {},
                    "total_trades": len(outcome_rows),
                    "confidence": "low",
                    "message": f"数据不足: {len(outcome_rows)}/{min_trades} 笔已平仓交易",
                }

            # 收集每个 decision 的因子投票
            decision_ids = [r["decision_id"] for r in outcome_rows]
            outcome_map = {r["decision_id"]: r["outcome_correct"] for r in outcome_rows}

            # 查询相关因子投票 (tech_trend, tech_momentum, tech_volume, tech_orderflow, tech_lifecycle)
            placeholders = ",".join("?" for _ in decision_ids)
            vote_rows = conn.execute(
                f"SELECT decision_id, model_id, direction FROM model_votes "
                f"WHERE decision_id IN ({placeholders}) "
                f"AND model_id LIKE 'tech_%'",
                decision_ids
            ).fetchall()

            # 按因子聚合: {factor_name: {"correct": N, "total": N}}
            factor_stats: Dict[str, Dict] = {}
            factor_name_map = {
                "tech_trend": "trend",
                "tech_momentum": "momentum",
                "tech_volume": "volume",
                "tech_orderflow": "orderflow",
                "tech_lifecycle": "lifecycle",
            }

            for vr in vote_rows:
                factor = factor_name_map.get(vr["model_id"], vr["model_id"])
                if factor not in factor_stats:
                    factor_stats[factor] = {"correct": 0, "total": 0}

                outcome = outcome_map.get(vr["decision_id"])
                if outcome is None:
                    continue

                factor_stats[factor]["total"] += 1
                # 因子投票方向与交易方向一致且盈利 → correct
                trade_dir = ""
                for r in outcome_rows:
                    if r["decision_id"] == vr["decision_id"]:
                        trade_dir = r["direction"]
                        break

                is_correct = (outcome == 1)
                if is_correct:
                    factor_stats[factor]["correct"] += 1

            # 计算各因子准确率
            factor_accuracy = {}
            for factor, stats in factor_stats.items():
                if stats["total"] > 0:
                    factor_accuracy[factor] = round(stats["correct"] / stats["total"], 3)

            if not factor_accuracy:
                return {
                    "learned_weights": {},
                    "factor_accuracy": {},
                    "total_trades": len(outcome_rows),
                    "confidence": "low",
                    "message": "无因子投票数据",
                }

            # 准确率 → 权重映射 (base=0.20, range=[0.10, 0.35])
            avg_accuracy = sum(factor_accuracy.values()) / len(factor_accuracy)
            learned_weights = {}
            for factor, acc in factor_accuracy.items():
                deviation = acc - avg_accuracy
                base_weight = 1.0 / len(factor_accuracy)
                adjusted = round(base_weight + deviation * 0.5, 2)
                learned_weights[factor] = max(0.10, min(0.35, adjusted))

            # 归一化到总和=1.0
            total_w = sum(learned_weights.values())
            if total_w > 0:
                learned_weights = {
                    k: round(v / total_w, 3) for k, v in learned_weights.items()
                }

            confidence = "high" if len(outcome_rows) >= 30 else "medium"

            logger.info(
                f"[DecisionRecorder] 因子权重学习完成: {len(outcome_rows)} 笔交易, "
                f"accuracy={factor_accuracy}, weights={learned_weights}"
            )

            return {
                "learned_weights": learned_weights,
                "factor_accuracy": factor_accuracy,
                "total_trades": len(outcome_rows),
                "confidence": confidence,
            }

        except Exception as e:
            logger.error(f"[DecisionRecorder] 因子权重学习失败: {e}")
            return {"learned_weights": {}, "factor_accuracy": {}, "total_trades": 0, "confidence": "low", "message": str(e)}
        finally:
            conn.close()

    # ─── P2 G13: 时段-策略对齐自动校准 ─────────────────────────

    def learn_session_strategy_alignment(self, days: int = 30, min_trades: int = 10) -> Dict[str, Any]:
        """从 trade_outcomes 学习各 (session, strategy_type) 的实战胜率

        关联 trade_outcomes.entry_session + strategy_type → outcome_correct,
        计算每对 (session, strategy) 的实际胜率, 映射为 alignment score [0.2, 1.0]。

        Returns:
            {
                "alignment": {"opening_drive": {"trend_following": 0.70, ...}, ...},
                "stats": {"opening_drive.trend_following": {"trades": 5, "win_rate": 0.60}, ...},
                "total_trades": N,
                "confidence": "high" | "medium" | "low",
            }
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT entry_session, strategy_type, outcome_correct "
                "FROM trade_outcomes "
                "WHERE outcome_correct IN (0, 1) "
                "AND entry_session IS NOT NULL AND entry_session != '' "
                "AND strategy_type IS NOT NULL AND strategy_type != '' "
                "AND created_at >= ?",
                (cutoff,)
            ).fetchall()

            if len(rows) < min_trades:
                return {
                    "alignment": {},
                    "stats": {},
                    "total_trades": len(rows),
                    "confidence": "low",
                    "message": f"数据不足: {len(rows)}/{min_trades} 笔带 session+strategy 标签的已平仓交易",
                }

            # 聚合: (session, strategy) → {correct, total}
            pair_stats: Dict[str, Dict] = {}
            for r in rows:
                key = (r["entry_session"], r["strategy_type"])
                sk = f"{r['entry_session']}.{r['strategy_type']}"
                if sk not in pair_stats:
                    pair_stats[sk] = {"correct": 0, "total": 0, "session": r["entry_session"], "strategy": r["strategy_type"]}
                pair_stats[sk]["total"] += 1
                if r["outcome_correct"] == 1:
                    pair_stats[sk]["correct"] += 1

            # 计算胜率 → alignment score
            # alignment = 0.4 + win_rate × 0.6 → 范围 [0.4, 1.0]
            alignment: Dict[str, Dict[str, float]] = {}
            stats_output = {}
            for sk, st in pair_stats.items():
                win_rate = st["correct"] / st["total"] if st["total"] > 0 else 0.0
                align = round(0.4 + win_rate * 0.6, 2)
                session = st["session"]
                strategy = st["strategy"]
                if session not in alignment:
                    alignment[session] = {}
                alignment[session][strategy] = align
                stats_output[sk] = {"trades": st["total"], "win_rate": round(win_rate, 3), "alignment": align}

            # 填充缺失的 (session, strategy) 组合 (用默认 0.70)
            all_sessions = ["opening_drive", "morning", "midday", "afternoon", "closing_drive"]
            all_strategies = ["trend_following", "mean_reversion", "breakout"]
            for sess in all_sessions:
                if sess not in alignment:
                    alignment[sess] = {}
                for strat in all_strategies:
                    if strat not in alignment[sess]:
                        alignment[sess][strat] = 0.70

            confidence = "high" if len(rows) >= 30 else "medium"

            logger.info(
                f"[DecisionRecorder] 时段-策略对齐学习完成: {len(rows)} 笔交易, "
                f"{len(pair_stats)} 对 (session, strategy)"
            )

            return {
                "alignment": alignment,
                "stats": stats_output,
                "total_trades": len(rows),
                "confidence": confidence,
            }

        except Exception as e:
            logger.error(f"[DecisionRecorder] 时段-策略对齐学习失败: {e}")
            return {"alignment": {}, "stats": {}, "total_trades": 0, "confidence": "low", "message": str(e)}
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
