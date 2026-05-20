"""
ReviewDBWriter — 复盘数据库写入器 (2026-05-04)

从 ftseek.db / execution.db 真实数据填充 ftseek_review.db。
涵盖 6 维度复盘 (决策/时机/持仓/风控/心理/系统)。

使用方式:
    writer = ReviewDBWriter()
    count = writer.populate_trades_review(date="2026-04-22")
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.review.db_writer")

FTSEEK_DB = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")
REVIEW_DB = os.environ.get("FTSEEK_REVIEW_DB_PATH", "data/ftseek_review.db")
EXECUTION_DB = os.environ.get("FTSEEK_EXECUTION_DB_PATH", "data/execution.db")


class ReviewDBWriter:
    """复盘数据库写入器 — 将交易数据转换为 6 维度复盘记录"""

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._ftseek_db = os.path.join(base_dir, FTSEEK_DB)
        self._review_db = os.path.join(base_dir, REVIEW_DB)
        self._exec_db = os.path.join(base_dir, EXECUTION_DB)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def populate_all(self, date: Optional[str] = None) -> Dict[str, int]:
        """填充所有复盘表"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        result = {
            "trades_review": self.populate_trades_review(date),
            "trade_reviews": self.populate_trade_reviews(date),
            "strategy_performance": self.populate_strategy_performance(date),
            "best_worst": self.populate_best_worst(date),
        }
        logger.info(f"[ReviewDBWriter] populate_all({date}): {result}")
        return result

    def populate_trades_review(self, date: Optional[str] = None) -> int:
        """填充 trades_review 表 — 从 ftseek.db 读取并映射到 6 维度"""
        trades = self._read_source_trades(date)
        if not trades:
            logger.info(f"[ReviewDBWriter] No trades to populate for {date}")
            return 0

        with sqlite3.connect(self._review_db) as conn:
            count = 0
            for t in trades:
                try:
                    trade_id = str(t.get("id", uuid.uuid4().hex[:12]))
                    code = t.get("code", "")

                    # 从 trade_attribution 获取质量评分
                    attr = self._get_attribution_scores(code, t.get("entry_time", ""))

                    # 从 trade_evaluations 获取执行质量
                    evals = self._get_trade_evaluations(code, t.get("entry_time", ""))

                    # 从 scenario_logs 获取市场环境
                    scenario = self._get_scenario_context(code, t.get("entry_time", ""))

                    # 构建 6 维度记录
                    row = {
                        "id": trade_id,
                        "signal_id": t.get("signal_id", ""),
                        "code": code,
                        "name": t.get("name", ""),
                        "action": t.get("action", ""),
                        "entry_price": t.get("entry_price", 0),
                        "exit_price": t.get("exit_price"),
                        "quantity": t.get("quantity", 0),
                        "pnl": t.get("pnl"),
                        "pnl_percent": t.get("pnl_percent"),
                        "fees": t.get("fees", 0),
                        "strategy": t.get("strategy", ""),
                        "entry_time": t.get("entry_time"),
                        "exit_time": t.get("exit_time"),
                        "exit_reason": t.get("exit_reason", ""),
                        "status": t.get("status", "open"),
                        "account_type": "real",
                        # 决策维度
                        "decision_reason": attr.get("decision_type", "technical"),
                        "decision_confidence": self._score_to_1_10(attr.get("direction_score", 0.5)),
                        "strategy_type": t.get("strategy", ""),
                        "decision_quality_score": self._score_to_1_10(attr.get("overall_score", 0.5)),
                        # 时机维度
                        "entry_timing_score": self._score_to_1_10(evals.get("entry_quality", 0.5)),
                        "exit_timing_score": self._score_to_1_10(evals.get("exit_quality", 0.5)),
                        "market_phase": scenario.get("scenario", ""),
                        # 持仓维度
                        "position_size_actual": attr.get("position_score", 0),
                        "holding_period_days": self._calc_holding_days(t.get("entry_time"), t.get("exit_time")),
                        # 风控维度
                        "stop_loss_planned": t.get("stop_loss"),
                        "stop_loss_executed": 1 if t.get("exit_reason", "") == "止损" else 0,
                        "max_drawdown_during_trade": evals.get("max_drawdown_pct"),
                        "risk_reward_actual": self._calc_rr(t.get("pnl", 0), evals.get("max_drawdown_pct", 0)),
                        # 心理维度
                        "emotion_state": "calm",
                        "confidence_entry": self._score_to_1_10(attr.get("signal_score", 0.5)),
                        "confidence_exit": self._score_to_1_10(evals.get("exit_quality", 0.5)),
                    }
                    self._upsert(conn, "trades_review", row)
                    count += 1
                except Exception as e:
                    logger.warning(f"[ReviewDBWriter] Failed to write trade review: {e}")

            conn.commit()
        logger.info(f"[ReviewDBWriter] trades_review populated: {count} records")
        return count

    def populate_trade_reviews(self, date: Optional[str] = None) -> int:
        """生成 trade_reviews — 基于 trades_review 的维度评分汇总"""
        with sqlite3.connect(self._review_db) as conn:
            conn.row_factory = sqlite3.Row

            if date:
                cur = conn.execute(
                    "SELECT * FROM trades_review WHERE date(entry_time) = ?", (date,)
                )
            else:
                cur = conn.execute("SELECT * FROM trades_review")

            rows = cur.fetchall()
            count = 0
            for r in rows:
                try:
                    scores = [
                        r["decision_quality_score"] or 5,
                        r["entry_timing_score"] or 5,
                        r["exit_timing_score"] or 5,
                        r["confidence_entry"] or 5,
                        r["confidence_exit"] or 5,
                    ]
                    overall = int(sum(scores) / len(scores))

                    pnl = r["pnl"] or 0
                    tags = []
                    if pnl > 0:
                        tags.append("盈利交易")
                    if r["stop_loss_executed"]:
                        tags.append("已止损")
                    if r["exit_reason"] == "止盈":
                        tags.append("止盈执行")

                    review_id = f"REV_{r['id']}"
                    row_data = {
                        "id": review_id,
                        "trade_id": r["id"],
                        "review_date": date or datetime.now().strftime("%Y-%m-%d"),
                        "overall_score": overall,
                        "decision_score": r["decision_quality_score"] or 5,
                        "timing_score": int(((r["entry_timing_score"] or 5) + (r["exit_timing_score"] or 5)) / 2),
                        "position_score": min(int((r["position_size_actual"] or 0) * 50), 10),
                        "risk_score": 8 if r["stop_loss_executed"] else 5,
                        "psychology_score": int(((r["confidence_entry"] or 5) + (r["confidence_exit"] or 5)) / 2),
                        "what_went_well": "纪律执行良好" if r["stop_loss_executed"] else "",
                        "what_went_wrong": "止损未执行" if not r["stop_loss_executed"] and pnl < 0 else "",
                        "key_learnings": f"{r['strategy']}: {r['exit_reason']}",
                        "tags": json.dumps(tags, ensure_ascii=False),
                    }
                    self._upsert(conn, "trade_reviews", row_data)
                    count += 1
                except Exception as e:
                    logger.warning(f"[ReviewDBWriter] Failed to write trade_review: {e}")

            conn.commit()
        logger.info(f"[ReviewDBWriter] trade_reviews populated: {count} records")
        return count

    def populate_strategy_performance(self, date: Optional[str] = None) -> int:
        """按策略聚合绩效统计"""
        with sqlite3.connect(self._ftseek_db) as conn_src:
            conn_src.row_factory = sqlite3.Row

            query = """
                SELECT strategy, COUNT(*) as total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                       SUM(pnl) as total_pnl,
                       AVG(pnl) as avg_pnl,
                       MAX(pnl) as max_win, MIN(pnl) as max_loss
                FROM trades WHERE strategy IS NOT NULL AND strategy != ''
            """
            if date:
                query += " AND date(entry_time) = ?"
                cur = conn_src.execute(query, (date,))
            else:
                cur = conn_src.execute(query)

            rows = cur.fetchall()

        with sqlite3.connect(self._review_db) as conn_dst:
            count = 0
            period = date or datetime.now().strftime("%Y-%m-%d")
            for r in rows:
                try:
                    total = r["total"] or 0
                    wins = r["wins"] or 0
                    win_rate = wins / total if total > 0 else 0
                    row_data = {
                        "id": f"SP_{r['strategy']}_{period}",
                        "strategy_name": r["strategy"],
                        "period_start": period,
                        "period_end": period,
                        "total_trades": total,
                        "winning_trades": wins,
                        "losing_trades": r["losses"] or 0,
                        "total_pnl": r["total_pnl"] or 0,
                        "avg_pnl": r["avg_pnl"] or 0,
                        "max_win": r["max_win"] or 0,
                        "max_loss": r["max_loss"] or 0,
                        "win_rate": win_rate,
                        "profit_factor": self._calc_profit_factor(r),
                        "strategy_type": r["strategy"],
                    }
                    self._upsert(conn_dst, "strategy_performance", row_data)
                    count += 1
                except Exception as e:
                    logger.warning(f"[ReviewDBWriter] Failed strategy perf: {e}")

            conn_dst.commit()
        logger.info(f"[ReviewDBWriter] strategy_performance: {count} records")
        return count

    def populate_best_worst(self, date: Optional[str] = None) -> int:
        """提取最佳/最差交易"""
        with sqlite3.connect(self._ftseek_db) as conn:
            conn.row_factory = sqlite3.Row

            query = "SELECT * FROM trades WHERE pnl IS NOT NULL"
            params = []
            if date:
                query += " AND date(entry_time) = ?"
                params.append(date)
            query += " ORDER BY pnl_percent DESC"

            cur = conn.execute(query, params)
            rows = cur.fetchall()

        if not rows:
            return 0

        with sqlite3.connect(self._review_db) as conn_dst:
            count = 0
            period = date or "all_time"

            # Best: top 3 (only trades with exit_price)
            valid = [r for r in rows if r["exit_price"]]
            best = [r for r in valid if (r["pnl_percent"] or 0) > 0][:3]
            worst = [r for r in valid if (r["pnl_percent"] or 0) < 0][-3:]

            for i, r in enumerate(best):
                row_data = self._best_worst_row(r, "best", period, i)
                self._upsert(conn_dst, "best_worst_trades", row_data)
                count += 1

            for i, r in enumerate(worst):
                row_data = self._best_worst_row(r, "worst", period, i)
                self._upsert(conn_dst, "best_worst_trades", row_data)
                count += 1

            conn_dst.commit()
        logger.info(f"[ReviewDBWriter] best_worst: {count} records")
        return count

    # ------------------------------------------------------------------
    #  Data Source Reads
    # ------------------------------------------------------------------

    def _read_source_trades(self, date: Optional[str] = None) -> List[Dict]:
        """从 ftseek.db 读取交易数据"""
        if not os.path.exists(self._ftseek_db):
            return []

        with sqlite3.connect(self._ftseek_db) as conn:
            conn.row_factory = sqlite3.Row

            if date:
                cur = conn.execute(
                    "SELECT * FROM trades WHERE date(entry_time) = ? OR date(exit_time) = ?",
                    (date, date)
                )
            else:
                cur = conn.execute("SELECT * FROM trades")

            rows = [dict(r) for r in cur.fetchall()]
        return rows

    def _get_attribution_scores(self, code: str, entry_time: str) -> Dict:
        """从 trade_attribution 获取归因评分"""
        if not os.path.exists(self._ftseek_db):
            return {}
        try:
            with sqlite3.connect(self._ftseek_db) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM trade_attribution WHERE code = ? ORDER BY timestamp DESC LIMIT 1",
                    (code,)
                )
                row = cur.fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}

    def _get_trade_evaluations(self, code: str, entry_time: str) -> Dict:
        """从 trade_evaluations 获取执行质量"""
        if not os.path.exists(self._ftseek_db):
            return {}
        try:
            with sqlite3.connect(self._ftseek_db) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM trade_evaluations WHERE code = ? LIMIT 1", (code,)
                )
                row = cur.fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}

    def _get_scenario_context(self, code: str, entry_time: str) -> Dict:
        """从 scenario_logs 获取市场环境"""
        if not os.path.exists(self._ftseek_db):
            return {}
        try:
            with sqlite3.connect(self._ftseek_db) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM scenario_logs WHERE code = ? ORDER BY timestamp DESC LIMIT 1",
                    (code,)
                )
                row = cur.fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_to_1_10(score: Optional[float]) -> int:
        """将 0-1 分数映射为 1-10"""
        if score is None:
            return 5
        return max(1, min(10, int(score * 10)))

    @staticmethod
    def _calc_holding_days(entry: Optional[str], exit_: Optional[str]) -> Optional[int]:
        if not entry:
            return None
        try:
            e1 = datetime.fromisoformat(str(entry).replace("Z", "+00:00"))
            e2 = datetime.fromisoformat(str(exit_).replace("Z", "+00:00")) if exit_ else datetime.now()
            return (e2 - e1).days
        except Exception:
            return None

    @staticmethod
    def _calc_rr(pnl: float, max_dd: float) -> Optional[float]:
        if not max_dd or max_dd == 0:
            return None
        return abs(pnl / max_dd)

    @staticmethod
    def _calc_profit_factor(row) -> float:
        wins = row["total_pnl"] if row["total_pnl"] and row["total_pnl"] > 0 else 0
        # rough estimate
        return round(abs(wins / max(abs(row["max_loss"] or 1), 1)), 2)

    def _best_worst_row(self, r, rec_type: str, period: str, idx: int) -> Dict:
        pnl_pct = r["pnl_percent"] if "pnl_percent" in r.keys() else 0
        return {
            "id": f"BW_{rec_type}_{period}_{idx}",
            "trade_id": str(r["id"]),
            "record_type": rec_type,
            "period": period,
            "code": r["code"],
            "action": r["action"] if "action" in r.keys() else "",
            "entry_price": r["entry_price"] if "entry_price" in r.keys() else 0,
            "exit_price": r["exit_price"] if "exit_price" in r.keys() and r["exit_price"] else r["entry_price"],
            "pnl": r["pnl"] if "pnl" in r.keys() else 0,
            "pnl_percent": pnl_pct,
            "highlight": "高盈利" if rec_type == "best" else "最大亏损",
            "key_factor": r["exit_reason"] if "exit_reason" in r.keys() else "",
            "record_date": datetime.now().strftime("%Y-%m-%d"),
        }

    @staticmethod
    def _upsert(conn: sqlite3.Connection, table: str, data: Dict):
        """INSERT OR REPLACE"""
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
            list(data.values()),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    writer = ReviewDBWriter()
    # 回填 2026-04-22 数据
    result = writer.populate_all(date="2026-04-22")
    print(f"Populated: {result}")
