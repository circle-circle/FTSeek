"""
统一交易记录存储 (Unified Trade Record Store)

⚠️ DEPRECATED (2026-06-12): Scalping 已禁用 (ADR-002), 此模块为孤儿代码.
写入操作已重定向到 TradeStore (ftseek_us.db SSOT).
请勿在此模块添加新功能 — 所有 trade_records 写入应统一走 TradeStore.

原始设计原则 (Karpathy):
  - 所有成交、建议、预测记录在同一张表中
  - 按 source_type 标注来源分类
  - 记录下单理由 (entry_reason) 和退出理由 (exit_reason)
  - 支持后续分析对比、归因和回归

来源分类:
  - MAIN_PIPELINE: 主预测管线 (LLM投票 → 信号 → 成交)
  - SCALPING: 剥头皮均值回归引擎 (纯技术指标)
  - LLM_PLAN: 盘前计划 (预测性，可能未执行)
  - LLM_RECOMMEND: LLM 推荐 (建议性，不一定成交)
  - MANUAL: 人工干预

表结构 (trade_records 扩展):
  + source_type:        来源分类
  + entry_reason:       下单理由 (JSON 数组: ["rsi", "vwap"] 或 ["llm_vote:3/3"])
  + signal_details:     信号详情 (JSON: RSI值/VWAP偏离度/投票结果等)
  + metadata_json:      扩展元数据 (策略ID、新闻ID、归因标签等)

现有字段保留:
  trade_date, symbol, direction, entry_time, exit_time,
  entry_price, exit_price, quantity, realized_pnl, pnl_pct,
  exit_reason, holding_hours, signal_strength, signal_confidence,
  voting_result, model_scores, decision_reason, trace_id, ...
"""

import sqlite3
import logging as _logging
_logger = _logging.getLogger("ftseek_us.unified_trade_store")
_logger.warning(
    "[DEPRECATED] UnifiedTradeStore 已废弃 — scalping 已禁用 (ADR-002). "
    "写入重定向到 TradeStore. 请勿添加新功能."
)
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── 来源类型枚举 ──────────────────────────────────────────────
SOURCE_MAIN_PIPELINE = "MAIN_PIPELINE"   # 主预测管线 (成交)
SOURCE_SCALPING = "SCALPING"              # 剥头皮引擎 (成交)
SOURCE_LLM_PLAN = "LLM_PLAN"              # 盘前计划 (预测)
SOURCE_LLM_RECOMMEND = "LLM_RECOMMEND"    # LLM 推荐 (建议)
SOURCE_MANUAL = "MANUAL"                   # 人工干预

# 成交 vs 非成交
EXECUTED_SOURCES = {SOURCE_MAIN_PIPELINE, SOURCE_SCALPING, SOURCE_MANUAL}
PREDICTED_SOURCES = {SOURCE_LLM_PLAN, SOURCE_LLM_RECOMMEND}

# 下单理由标签
ENTRY_RSI = "rsi"
ENTRY_VWAP = "vwap"
ENTRY_BOLLINGER = "bollinger"
ENTRY_LLM_VOTE = "llm_vote"
ENTRY_NEWS_EVENT = "news_event"
ENTRY_TECHNICAL = "technical"
ENTRY_MANUAL = "manual"

# 退出理由标签
EXIT_TP = "tp_hit"
EXIT_SL = "sl_hit"
EXIT_TIME = "time_stop"
EXIT_MANUAL = "manual_close"
EXIT_SIGNAL_REVERSAL = "signal_reversal"
EXIT_NEWS = "news_exit"


class UnifiedTradeRecord:
    """
    统一交易记录 DTO
    
    涵盖:
      - 成交记录: 有 entry_price, exit_price, realized_pnl
      - 预测记录: 有 entry_price 但无 exit (待验证)
      - 建议记录: 有 symbol, direction, 但无价格
    """

    def __init__(
        self,
        symbol: str,
        direction: str,
        source_type: str,
        # 可选字段
        trade_date: Optional[str] = None,
        entry_time: Optional[str] = None,
        exit_time: Optional[str] = None,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        quantity: Optional[int] = None,
        realized_pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        # 理由标签
        entry_reason: Optional[List[str]] = None,
        exit_reason: Optional[str] = None,
        # 信号详情
        signal_strength: Optional[int] = None,
        signal_confidence: Optional[float] = None,
        signal_details: Optional[Dict] = None,
        # 投票/模型
        voting_result: Optional[str] = None,
        model_scores: Optional[str] = None,
        consensus_level: Optional[float] = None,
        # 策略/追踪
        strategy_id: Optional[str] = None,
        decision_reason: Optional[str] = None,
        trace_id: Optional[str] = None,
        futu_order_id: Optional[str] = None,
        close_futu_order_id: Optional[str] = None,
        # 扩展元数据
        metadata: Optional[Dict] = None,
        # O4/O5 (2026-05-16): owner_strategy + final_strength + market_regime + mode 落库
        # 修复 5/15 22/26 trades owner_strategy NULL + voting final/consensus NULL
        owner_strategy: Optional[str] = None,
        final_strength: Optional[int] = None,
        market_regime: Optional[str] = None,
        mode: Optional[str] = None,
    ):
        self.symbol = symbol
        self.direction = direction
        self.source_type = source_type
        # P0 修复 (2026-05-07): 使用 ET 日期而非 CST，避免复盘查询时区错位
        self.trade_date = trade_date or (
            datetime.now(timezone(timedelta(hours=-4)))
        ).strftime("%Y-%m-%d")
        self.entry_time = entry_time or datetime.now().isoformat()
        self.exit_time = exit_time
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.quantity = quantity
        self.realized_pnl = realized_pnl
        self.pnl_pct = pnl_pct
        self.entry_reason = entry_reason or []
        self.exit_reason = exit_reason
        self.signal_strength = signal_strength
        self.signal_confidence = signal_confidence
        self.signal_details = signal_details or {}
        self.voting_result = voting_result
        self.model_scores = model_scores
        self.consensus_level = consensus_level
        self.strategy_id = strategy_id
        self.decision_reason = decision_reason
        self.trace_id = trace_id
        self.futu_order_id = futu_order_id
        self.close_futu_order_id = close_futu_order_id
        self.metadata = metadata or {}
        # O4/O5
        self.owner_strategy = owner_strategy
        self.final_strength = final_strength
        self.market_regime = market_regime
        self.mode = mode

    def to_dict(self) -> Dict:
        """转换为数据库插入字典"""
        from datetime import datetime
        return {
            "trade_date": self.trade_date,
            "symbol": self.symbol,
            "direction": self.direction,
            "source_type": self.source_type,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "realized_pnl": self.realized_pnl,
            "pnl_pct": self.pnl_pct,
            "entry_reason": json.dumps(self.entry_reason, ensure_ascii=False) if self.entry_reason else None,
            "exit_reason": self.exit_reason,
            "signal_strength": self.signal_strength,
            "signal_confidence": self.signal_confidence,
            "signal_details": json.dumps(self.signal_details, ensure_ascii=False) if self.signal_details else None,
            "voting_result": self.voting_result,
            "model_scores": self.model_scores,
            "consensus_level": self.consensus_level,
            "strategy_id": self.strategy_id,
            "decision_reason": self.decision_reason,
            "trace_id": self.trace_id,
            "futu_order_id": self.futu_order_id,
            "close_futu_order_id": self.close_futu_order_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False) if self.metadata else None,
            # O4/O5 (2026-05-16): 4 列回填
            "owner_strategy": self.owner_strategy,
            "final_strength": self.final_strength,
            "market_regime": self.market_regime,
            "mode": self.mode,
        }

    @classmethod
    def from_row(cls, row: dict) -> "UnifiedTradeRecord":
        """从数据库行重建对象"""
        entry_reason = []
        if row.get("entry_reason"):
            try:
                entry_reason = json.loads(row["entry_reason"])
            except (json.JSONDecodeError, TypeError):
                entry_reason = [row["entry_reason"]] if row["entry_reason"] else []

        signal_details = {}
        if row.get("signal_details"):
            try:
                signal_details = json.loads(row["signal_details"])
            except (json.JSONDecodeError, TypeError):
                pass

        metadata = {}
        if row.get("metadata_json"):
            try:
                metadata = json.loads(row["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        return cls(
            symbol=row.get("symbol", ""),
            direction=row.get("direction", ""),
            source_type=row.get("source_type", SOURCE_MAIN_PIPELINE),
            trade_date=row.get("trade_date"),
            entry_time=row.get("entry_time"),
            exit_time=row.get("exit_time"),
            entry_price=row.get("entry_price"),
            exit_price=row.get("exit_price"),
            quantity=row.get("quantity"),
            realized_pnl=row.get("realized_pnl"),
            pnl_pct=row.get("pnl_pct"),
            entry_reason=entry_reason,
            exit_reason=row.get("exit_reason"),
            signal_strength=row.get("signal_strength"),
            signal_confidence=row.get("signal_confidence"),
            signal_details=signal_details,
            voting_result=row.get("voting_result"),
            model_scores=row.get("model_scores"),
            consensus_level=row.get("consensus_level"),
            strategy_id=row.get("strategy_id"),
            decision_reason=row.get("decision_reason"),
            trace_id=row.get("trace_id"),
            futu_order_id=row.get("futu_order_id"),
            close_futu_order_id=row.get("close_futu_order_id"),
            metadata=metadata,
        )


class UnifiedTradeStore:
    """
    统一交易记录存储管理器
    
    职责:
      1. 自动迁移 schema (添加 source_type 等新列)
      2. 统一写入接口 (所有来源用同一方法)
      3. 按来源查询/统计/对比
      4. 归因分析支持
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()
        logger.info(f"[UnifiedTradeStore] 已初始化 (db={db_path})")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        """自动迁移 schema — 添加缺失列 (或创建表如果不存在)"""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()

            # 检查表是否存在
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_records'"
            )
            table_exists = cursor.fetchone() is not None

            if not table_exists:
                # 创建完整表结构
                cursor.execute("""
                    CREATE TABLE trade_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        entry_time TEXT,
                        exit_time TEXT,
                        entry_price REAL,
                        exit_price REAL,
                        quantity INTEGER,
                        realized_pnl REAL,
                        pnl_pct REAL,
                        entry_reason TEXT,
                        exit_reason TEXT,
                        holding_hours REAL,
                        signal_strength INTEGER,
                        signal_confidence REAL,
                        signal_details TEXT,
                        voting_result TEXT,
                        model_scores TEXT,
                        consensus_level REAL,
                        strategy_id TEXT,
                        decision_reason TEXT,
                        trace_id TEXT,
                        futu_order_id TEXT,
                        close_futu_order_id TEXT,
                        source_type TEXT DEFAULT 'MAIN_PIPELINE',
                        metadata_json TEXT,
                        max_drawdown REAL,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        final_strength INTEGER,
                        min_confidence REAL
                    )
                """)
                logger.info("[UnifiedTradeStore] trade_records 表已创建")
            else:
                # 添加缺失列
                cursor.execute("PRAGMA table_info(trade_records)")
                existing_cols = {row[1] for row in cursor.fetchall()}

                new_cols = {
                    "source_type": "TEXT DEFAULT 'MAIN_PIPELINE'",
                    "entry_reason": "TEXT",
                    "signal_details": "TEXT",
                    "metadata_json": "TEXT",
                }

                for col_name, col_def in new_cols.items():
                    if col_name not in existing_cols:
                        cursor.execute(
                            f"ALTER TABLE trade_records ADD COLUMN {col_name} {col_def}"
                        )
                        logger.info(f"[UnifiedTradeStore] 新增列: {col_name}")

            conn.commit()

            # 创建索引
            indexes = [
                ("idx_source_type", "trade_records", "source_type"),
                ("idx_entry_reason", "trade_records", "entry_reason"),
                ("idx_symbol_source", "trade_records", "symbol, source_type"),
                ("idx_trade_date_source", "trade_records", "trade_date, source_type"),
            ]
            for idx_name, table, cols in indexes:
                try:
                    cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({cols})")
                except sqlite3.OperationalError:
                    pass

            conn.commit()

        finally:
            conn.close()

    def insert(self, record: UnifiedTradeRecord) -> int:
        """
        插入交易记录 (所有来源统一接口)
        
        Returns:
            新记录的 ID
        """
        data = record.to_dict()
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                columns = list(data.keys())
                placeholders = ", ".join(["?" for _ in columns])
                col_names = ", ".join(columns)
                values = [data[c] for c in columns]

                cursor.execute(
                    f"INSERT INTO trade_records ({col_names}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
                record_id = cursor.lastrowid

                logger.info(
                    f"[UnifiedTradeStore] 记录插入 | ID={record_id} | "
                    f"{record.symbol} {record.direction} | "
                    f"来源: {record.source_type} | "
                    f"理由: {record.entry_reason}"
                )
                return record_id
            finally:
                conn.close()

    def update_exit(
        self,
        record_id: int,
        exit_price: float,
        exit_reason: str,
        realized_pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        metadata: Optional[Dict] = None,
    ):
        """更新退出信息 (平仓时调用)"""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                updates = {
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "exit_time": datetime.now().isoformat(),
                }
                if realized_pnl is not None:
                    updates["realized_pnl"] = realized_pnl
                if pnl_pct is not None:
                    updates["pnl_pct"] = pnl_pct
                if metadata:
                    # 合并现有 metadata
                    cursor.execute(
                        "SELECT metadata_json FROM trade_records WHERE id=?",
                        (record_id,),
                    )
                    row = cursor.fetchone()
                    existing_meta = {}
                    if row and row[0]:
                        try:
                            existing_meta = json.loads(row[0])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    existing_meta.update(metadata)
                    updates["metadata_json"] = json.dumps(existing_meta, ensure_ascii=False)

                set_clause = ", ".join(f"{k}=?" for k in updates.keys())
                values = list(updates.values()) + [record_id]

                cursor.execute(
                    f"UPDATE trade_records SET {set_clause} WHERE id=?",
                    values,
                )
                conn.commit()
            finally:
                conn.close()


    def rollback_failed_record(self, record_id: int, reason: str = "ORDER_FAILED"):
        """回滚失败的交易记录 — 下单前被拦截或下单失败时调用"""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                # 先读取现有 metadata
                cursor.execute(
                    "SELECT metadata_json FROM trade_records WHERE id=?",
                    (record_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    logger.warning(f"[UnifiedTradeStore] 回滚失败: 记录 ID={record_id} 不存在")
                    return
                existing_meta = {}
                if row[0]:
                    try:
                        existing_meta = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        pass
                existing_meta["rollback_reason"] = reason
                existing_meta["rollback_time"] = datetime.now().isoformat()
                
                cursor.execute(
                    "UPDATE trade_records SET exit_price=0, exit_reason=?, realized_pnl=0, "
                    "metadata_json=? WHERE id=?",
                    (f"ROLLBACK_{reason}", json.dumps(existing_meta, ensure_ascii=False), record_id),
                )
                conn.commit()
                logger.info(f"[UnifiedTradeStore] 记录回滚 | ID={record_id} | 原因: {reason}")
            finally:
                conn.close()

    def update_by_trace_id(
        self,
        trace_id: str,
        exit_price: float,
        exit_reason: str,
        realized_pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ):
        """通过 trace_id 更新退出信息"""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id FROM trade_records WHERE trace_id=?",
                    (trace_id,),
                )
                row = cursor.fetchone()
                if row:
                    self.update_exit(row[0], exit_price, exit_reason, realized_pnl, pnl_pct)
                else:
                    logger.warning(f"[UnifiedTradeStore] trace_id={trace_id} 未找到")
            finally:
                conn.close()

    def query(
        self,
        source_type: Optional[str] = None,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        only_executed: bool = False,
        only_predicted: bool = False,
        limit: int = 100,
    ) -> List[UnifiedTradeRecord]:
        """
        查询交易记录
        
        Args:
            source_type: 按来源过滤
            symbol: 按标的过滤
            trade_date: 按日期过滤
            date_from/date_to: 日期范围
            only_executed: 只查成交记录
            only_predicted: 只查预测记录
            limit: 限制返回数量
        """
        conditions = []
        params = []

        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if trade_date:
            conditions.append("trade_date = ?")
            params.append(trade_date)
        if date_from:
            conditions.append("trade_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("trade_date <= ?")
            params.append(date_to)
        if only_executed:
            conditions.append("source_type IN ('MAIN_PIPELINE', 'SCALPING', 'MANUAL')")
        if only_predicted:
            conditions.append("source_type IN ('LLM_PLAN', 'LLM_RECOMMEND')")

        where = " AND ".join(conditions) if conditions else "1=1"

        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            query = f"SELECT * FROM trade_records WHERE {where} ORDER BY entry_time DESC LIMIT ?"
            cursor.execute(query, params + [limit])
            rows = cursor.fetchall()
            return [UnifiedTradeRecord.from_row(dict(r)) for r in rows]
        finally:
            conn.close()

    def get_stats_by_source(
        self,
        trade_date: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Dict]:
        """
        按来源分类统计
        
        Returns:
            {
                "SCALPING": {"total_trades": N, "win_rate": X, "avg_pnl": Y, ...},
                "MAIN_PIPELINE": {...},
                ...
            }
        """
        conditions = ["exit_price IS NOT NULL", "source_type IS NOT NULL"]
        params = []

        if trade_date:
            conditions.append("trade_date = ?")
            params.append(trade_date)
        if date_from:
            conditions.append("trade_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("trade_date <= ?")
            params.append(date_to)

        where = " AND ".join(conditions)

        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            query = f"""
                SELECT 
                    source_type,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
                    AVG(pnl_pct) as avg_pnl_pct,
                    SUM(pnl_pct) as total_pnl_pct,
                    AVG(CASE WHEN pnl_pct > 0 THEN pnl_pct END) as avg_win,
                    AVG(CASE WHEN pnl_pct <= 0 THEN pnl_pct END) as avg_loss,
                    MAX(pnl_pct) as best_trade,
                    MIN(pnl_pct) as worst_trade,
                    AVG(
                        CASE WHEN exit_time IS NOT NULL AND entry_time IS NOT NULL
                        THEN (julianday(exit_time) - julianday(entry_time)) * 24
                        END
                    ) as avg_holding_hours
                FROM trade_records
                WHERE {where}
                GROUP BY source_type
            """
            cursor.execute(query, params)
            rows = cursor.fetchall()

            result = {}
            for row in rows:
                source = row["source_type"]
                total = row["total_trades"]
                wins = row["wins"]
                win_rate = wins / total if total > 0 else 0
                avg_pnl = row["avg_pnl_pct"] or 0
                avg_win = row["avg_win"] or 0
                avg_loss = row["avg_loss"] or 0
                expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss if avg_loss else win_rate * avg_win

                result[source] = {
                    "total_trades": total,
                    "wins": wins,
                    "losses": row["losses"],
                    "win_rate": win_rate,
                    "avg_pnl_pct": avg_pnl,
                    "total_pnl_pct": row["total_pnl_pct"] or 0,
                    "avg_win_pct": avg_win,
                    "avg_loss_pct": avg_loss,
                    "expectancy": expectancy,
                    "best_trade": row["best_trade"],
                    "worst_trade": row["worst_trade"],
                    "avg_holding_hours": row["avg_holding_hours"],
                }

            return result
        finally:
            conn.close()

    def get_entry_reason_stats(
        self,
        source_type: Optional[str] = None,
        trade_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        按下单理由统计胜率
        
        用于归因分析: 哪些信号类型表现最好?
        """
        conditions = ["exit_price IS NOT NULL", "entry_reason IS NOT NULL"]
        params = []

        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)
        if trade_date:
            conditions.append("trade_date = ?")
            params.append(trade_date)

        where = " AND ".join(conditions)

        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT entry_reason,
                       COUNT(*) as total,
                       SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                       AVG(pnl_pct) as avg_pnl
                FROM trade_records
                WHERE {where}
                GROUP BY entry_reason
                ORDER BY total DESC
                """,
                params,
            )
            rows = cursor.fetchall()

            result = []
            for row in rows:
                reasons = []
                try:
                    reasons = json.loads(row["entry_reason"])
                except (json.JSONDecodeError, TypeError):
                    reasons = [row["entry_reason"]] if row["entry_reason"] else []

                total = row["total"]
                win_rate = row["wins"] / total if total > 0 else 0
                result.append({
                    "entry_reasons": reasons,
                    "total": total,
                    "wins": row["wins"],
                    "win_rate": win_rate,
                    "avg_pnl": row["avg_pnl"] or 0,
                })

            return result
        finally:
            conn.close()

    def get_prediction_accuracy(
        self,
        source_type: str = SOURCE_LLM_PLAN,
        trade_date: Optional[str] = None,
    ) -> Dict:
        """
        预测准确率分析
        
        对比 LLM 预测 (entry_price, direction) 与实际成交结果的偏差
        """
        conditions = ["source_type = ?", "exit_price IS NOT NULL"]
        params = [source_type]

        if trade_date:
            conditions.append("trade_date = ?")
            params.append(trade_date)

        where = " AND ".join(conditions)

        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT symbol, direction, entry_price, exit_price, pnl_pct
                FROM trade_records
                WHERE {where}
                """,
                params,
            )
            rows = cursor.fetchall()

            if not rows:
                return {"total_predictions": 0}

            total = len(rows)
            correct_direction = sum(1 for r in rows if (
                (r["direction"] == "LONG" and r["pnl_pct"] > 0) or
                (r["direction"] == "SHORT" and r["pnl_pct"] > 0)
            ))

            return {
                "total_predictions": total,
                "correct_direction": correct_direction,
                "accuracy": correct_direction / total if total > 0 else 0,
                "avg_pnl": sum(r["pnl_pct"] or 0 for r in rows) / total,
                "best_prediction": max((r["pnl_pct"] or 0) for r in rows),
                "worst_prediction": min((r["pnl_pct"] or 0) for r in rows),
            }
        finally:
            conn.close()

    def get_cross_source_comparison(
        self,
        trade_date: Optional[str] = None,
    ) -> Dict:
        """
        跨来源对比分析
        
        对比不同来源 (Scalping vs Main Pipeline vs LLM Plan) 的表现
        """
        stats = self.get_stats_by_source(trade_date=trade_date)
        prediction = self.get_prediction_accuracy(trade_date=trade_date)
        reason_stats = self.get_entry_reason_stats(trade_date=trade_date)

        return {
            "source_comparison": stats,
            "llm_prediction": prediction,
            "entry_reason_analysis": reason_stats,
            "summary": {
                "best_source": max(stats.items(), key=lambda x: x[1].get("win_rate", 0))[0] if stats else "N/A",
                "worst_source": min(stats.items(), key=lambda x: x[1].get("win_rate", 0))[0] if stats else "N/A",
                "total_executed": sum(s["total_trades"] for s in stats.values() if s.get("total_trades")),
                "overall_win_rate": sum(s["wins"] for s in stats.values()) / sum(s["total_trades"] for s in stats.values()) if any(s.get("total_trades") for s in stats.values()) else 0,
            },
        }


# ─── 单例获取 ─────────────────────────────────────────────────
_instance: Optional[UnifiedTradeStore] = None
_instance_lock = threading.Lock()


def get_unified_trade_store(db_path: str = None) -> UnifiedTradeStore:
    """获取 UnifiedTradeStore 单例"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = UnifiedTradeStore(db_path)
    return _instance


def reset_unified_trade_store():
    """重置单例 (用于测试)"""
    global _instance
    with _instance_lock:
        _instance = None
