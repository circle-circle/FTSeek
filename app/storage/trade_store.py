"""
交易记录持久化存储

将每笔完整交易（开仓→平仓）写入 SQLite trade_records 表，
重启后历史不丢失，支持日报复盘查询。
"""
import sqlite3
import threading
import logging
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_runtime_mode() -> str:
    """运行时取 mode (sandbox / normal). 失败默认 normal."""
    try:
        from app.core.config_container import get_config
        if get_config().get_dotted("mode.sandbox.enabled", False):
            return "sandbox"
    except Exception:
        pass
    return "normal"


class TradeStore:
    """
    持久化交易记录存储

    schema:
        trade_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date      TEXT,    -- 开仓日期 YYYY-MM-DD
            symbol          TEXT,
            direction       TEXT,    -- LONG / SHORT
            entry_time      TEXT,
            exit_time       TEXT,
            entry_price     REAL,
            exit_price      REAL,
            quantity        INTEGER,
            realized_pnl    REAL,
            pnl_pct         REAL,
            exit_reason     TEXT,    -- TP1/TP2/TP3/SL/TRAILING/TIMEOUT
            holding_hours   REAL,
            signal_strength INTEGER,
            signal_confidence REAL,
            futu_order_id   TEXT,
            created_at      TEXT
        )
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # P0 修复 (2026-04-16): 启用 WAL 模式，防止多进程并发锁死
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        return conn

    def _init_db(self):
        """创建 trade_records / open_positions 表（幂等）"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS trade_records (
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date        TEXT NOT NULL,
                        symbol            TEXT NOT NULL,
                        direction         TEXT NOT NULL,
                        entry_time        TEXT,
                        exit_time         TEXT,
                        entry_price       REAL,
                        exit_price        REAL,
                        quantity          INTEGER,
                        realized_pnl      REAL,
                        pnl_pct           REAL,
                        exit_reason       TEXT,
                        holding_hours     REAL,
                        signal_strength   INTEGER,
                        signal_confidence REAL,
                        futu_order_id     TEXT,
                        -- 投票详情字段 (新增)
                        voting_result     TEXT,    -- JSON: 投票详情
                        final_strength    INTEGER, -- 最终评分
                        consensus_level   REAL,    -- 一致性水平
                        model_scores      TEXT,    -- JSON: 各模型评分
                        -- 决策信息 (新增)
                        strategy_id       TEXT,    -- 策略 ID
                        decision_reason   TEXT,    -- 决策原因
                        -- 风控信息 (新增)
                        max_drawdown      REAL,    -- 持仓期间最大回撤
                        trace_id          TEXT,    -- 全链路归因 ID (Phase 2)
                        created_at        TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_date ON trade_records(trade_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON trade_records(symbol)")

                # P0 Fix 2026-04-28: 添加平仓单 futu_order_id 列 (向后兼容)
                try:
                    conn.execute("ALTER TABLE trade_records ADD COLUMN close_futu_order_id TEXT")
                except Exception:
                    pass  # 列已存在

                # Karpathy 统一记录: 添加来源分类 + 理由字段 (2026-04-29)
                # Phase A (2026-05-14): 加 owner_strategy 列, 与 source_type 互补
                #   - source_type: 来源大类 (MAIN_PIPELINE/SCALPING/...)
                #   - owner_strategy: 具体 strategy.name (main/scalping/fast_lane/llm_plan)
                for col_def in [
                    ("source_type", "ALTER TABLE trade_records ADD COLUMN source_type TEXT DEFAULT 'MAIN_PIPELINE'"),
                    ("entry_reason", "ALTER TABLE trade_records ADD COLUMN entry_reason TEXT"),
                    ("signal_details", "ALTER TABLE trade_records ADD COLUMN signal_details TEXT"),
                    ("metadata_json", "ALTER TABLE trade_records ADD COLUMN metadata_json TEXT"),
                    ("owner_strategy", "ALTER TABLE trade_records ADD COLUMN owner_strategy TEXT"),
                    # 沙盒模式标记 (2026-05-14): normal / sandbox
                    ("mode", "ALTER TABLE trade_records ADD COLUMN mode TEXT DEFAULT 'normal'"),
                    # O4 (2026-05-16): regime 显式落库 (Y1 RegimeDetector 数据复盘可见)
                    ("market_regime", "ALTER TABLE trade_records ADD COLUMN market_regime TEXT DEFAULT 'UNKNOWN'"),
                ]:
                    try:
                        conn.execute(col_def[1])
                        logger.info(f"[TradeStore] 新增统一记录列: {col_def[0]}")
                    except Exception:
                        pass  # 列已存在

                # 统一记录索引
                for idx_def in [
                    "CREATE INDEX IF NOT EXISTS idx_source_type ON trade_records(source_type)",
                    "CREATE INDEX IF NOT EXISTS idx_entry_reason ON trade_records(entry_reason)",
                    "CREATE INDEX IF NOT EXISTS idx_symbol_source ON trade_records(symbol, source_type)",
                    "CREATE INDEX IF NOT EXISTS idx_trade_date_source ON trade_records(trade_date, source_type)",
                    "CREATE INDEX IF NOT EXISTS idx_owner_strategy ON trade_records(owner_strategy)",
                    "CREATE INDEX IF NOT EXISTS idx_trade_mode ON trade_records(mode)",
                ]:
                    try:
                        conn.execute(idx_def)
                    except Exception:
                        pass

                # 期权持仓表（SHORT信号 → 买入Put期权，作为做空替代）
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS option_positions (
                        symbol          TEXT NOT NULL,
                        option_code     TEXT PRIMARY KEY,
                        direction       TEXT NOT NULL DEFAULT 'SHORT_PROXY',
                        qty             INTEGER NOT NULL,
                        entry_price     REAL NOT NULL,
                        strike_price    REAL,
                        expiry_date     TEXT,
                        entry_time      TEXT NOT NULL,
                        cost_total      REAL,
                        signal_strength INTEGER,
                        signal_confidence REAL,
                        is_simulated    INTEGER DEFAULT 0,
                        mode            TEXT NOT NULL DEFAULT 'normal',
                        updated_at      TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_opt_symbol ON option_positions(symbol)")

                # 未平仓持仓快照表（服务重启后恢复用）
                # P0-1 升级 (2026-04-21): 统一仓位状态真相源 (Truth Source of Record)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS open_positions (
                        symbol            TEXT PRIMARY KEY,
                        direction         TEXT NOT NULL,
                        size              REAL,
                        avg_entry_price   REAL,
                        initial_sl        REAL,
                        current_sl        REAL,
                        initial_tp        REAL,
                        take_profit_json  TEXT,  -- JSON array
                        trail_state_json  TEXT,  -- JSON object
                        entry_time        TEXT,
                        time_stop_hours   INTEGER,
                        strategy_id       TEXT,
                        signal_id         TEXT,
                        position_id       TEXT,
                        market_regime     TEXT,
                        -- 真相源字段 (P0-1 新增)
                        version           INTEGER DEFAULT 1,           -- 乐观锁版本号
                        source            TEXT DEFAULT 'scheduler',    -- 写入来源: scheduler/exit_monitor/sync
                        sync_status       TEXT DEFAULT 'synced',       -- 同步状态: synced/pending_sync/error
                        last_sync_at      TEXT,                        -- 最后同步时间
                        is_filled         INTEGER DEFAULT 1,           -- 是否已成交 (0=待成交, 1=已成交)
                        is_partially_reduced INTEGER DEFAULT 0,        -- 是否已部分减仓
                        is_anomalous      INTEGER DEFAULT 0,           -- 是否异常 (状态不一致)
                        trace_id          TEXT,                        -- 全链路归因 ID
                        action_log_json   TEXT,                        -- 最近动作日志 JSON array
                        updated_at        TEXT NOT NULL
                    )
                """)
                # P0-1 迁移: 为已存在的表添加新列 (幂等 ALTER)
                for col_def in [
                    ("version", "ALTER TABLE open_positions ADD COLUMN version INTEGER DEFAULT 1"),
                    ("source", "ALTER TABLE open_positions ADD COLUMN source TEXT DEFAULT 'scheduler'"),
                    ("sync_status", "ALTER TABLE open_positions ADD COLUMN sync_status TEXT DEFAULT 'synced'"),
                    ("last_sync_at", "ALTER TABLE open_positions ADD COLUMN last_sync_at TEXT"),
                    ("is_filled", "ALTER TABLE open_positions ADD COLUMN is_filled INTEGER DEFAULT 1"),
                    ("is_partially_reduced", "ALTER TABLE open_positions ADD COLUMN is_partially_reduced INTEGER DEFAULT 0"),
                    ("is_anomalous", "ALTER TABLE open_positions ADD COLUMN is_anomalous INTEGER DEFAULT 0"),
                    ("trace_id_new", "ALTER TABLE open_positions ADD COLUMN trace_id TEXT"),
                    ("action_log_json", "ALTER TABLE open_positions ADD COLUMN action_log_json TEXT"),
                    # Phase A (2026-05-14): 持仓的归属策略 (用于 ExitDispatcher 路由)
                    ("owner_strategy", "ALTER TABLE open_positions ADD COLUMN owner_strategy TEXT"),
                    ("mode", "ALTER TABLE open_positions ADD COLUMN mode TEXT DEFAULT 'normal'"),
                ]:
                    try:
                        conn.execute(col_def[1])
                    except Exception:
                        pass  # 列已存在，跳过

                # Phase A: open_positions 加 owner_strategy 索引
                try:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_open_owner ON open_positions(owner_strategy)")
                except Exception:
                    pass

                # ── 止损事件审计表 (Daemon 平仓记录, 2026-04-24) ──
                # v2 (2026-04-24 方案A): 补齐 entry_price/direction/size/trace_id 等
                # 使得 stoploss_events 可独立支撑复盘, 不再只依赖 trade_records 反查
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS stoploss_events (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_time      TEXT NOT NULL,
                        symbol          TEXT NOT NULL,
                        event_type      TEXT,   -- DYNAMIC_STOP/FLASH_EXIT/DEAD_MONEY/TIME_STOP/HARD_STOP
                        exit_price      REAL,
                        pnl_pct         REAL,
                        reason          TEXT,
                        atr             REAL,
                        daemon_pid      INTEGER
                    )
                """)
                # 幂等 ALTER: 为已存在的 stoploss_events 增加审计字段
                for col_def in [
                    ("entry_price", "ALTER TABLE stoploss_events ADD COLUMN entry_price REAL"),
                    ("entry_time",  "ALTER TABLE stoploss_events ADD COLUMN entry_time TEXT"),
                    ("direction",   "ALTER TABLE stoploss_events ADD COLUMN direction TEXT"),
                    ("size",        "ALTER TABLE stoploss_events ADD COLUMN size REAL"),
                    ("holding_hours", "ALTER TABLE stoploss_events ADD COLUMN holding_hours REAL"),
                    ("realized_pnl", "ALTER TABLE stoploss_events ADD COLUMN realized_pnl REAL"),
                    ("trace_id",    "ALTER TABLE stoploss_events ADD COLUMN trace_id TEXT"),
                    ("strategy_id", "ALTER TABLE stoploss_events ADD COLUMN strategy_id TEXT"),
                ]:
                    try:
                        conn.execute(col_def[1])
                    except Exception:
                        pass  # 列已存在
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sle_time ON stoploss_events(event_time DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sle_symbol ON stoploss_events(symbol)")

                # ── Daemon 心跳表 (UPSERT 单行, 2026-04-24) ──
                # 兼容旧版 id 主键的结构: 检测到则 DROP 重建为 daemon_name 主键
                try:
                    row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daemon_heartbeat'"
                    ).fetchone()
                    if row and row[0] and 'daemon_name' in row[0] and 'PRIMARY KEY' in row[0] \
                            and 'daemon_name     TEXT PRIMARY KEY' not in row[0] \
                            and 'daemon_name TEXT PRIMARY KEY' not in row[0]:
                        conn.execute("DROP TABLE daemon_heartbeat")
                        logger.info("[TradeStore] 迁移 daemon_heartbeat 到 daemon_name 主键结构")
                except Exception as _e:
                    pass
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daemon_heartbeat (
                        daemon_name     TEXT PRIMARY KEY,
                        pid             INTEGER,
                        last_heartbeat  TEXT NOT NULL,
                        status          TEXT,
                        position_count  INTEGER,
                        uptime_seconds  REAL
                    )
                """)

                # F19 (2026-05-12): 归因表完整 schema
                self._ensure_trade_attribution_schema(conn)

                conn.commit()
                logger.info("[TradeStore] 交易记录表初始化完成")
            finally:
                conn.close()

    def _ensure_trade_attribution_schema(self, conn: sqlite3.Connection):
        """
        F19 (2026-05-12): trade_attribution 完整 schema (集中管理)

        历史背景:
          - 旧版本: order_executor.py 内联 CREATE TABLE, 字段只有 7 个
                    (attribution_id/trace_id/trade_date/symbol/direction/strength/confidence)
          - 现状: daily_review 的 attribution 维度永远缺字段, 进化层无法用
          - 修复: 集中到 TradeStore, ALTER TABLE 幂等加全新字段, 加 3 个索引
        """
        # 基础表 (兼容已存在的最小 schema)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_attribution (
                attribution_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id       TEXT,
                trace_id          TEXT,
                trade_date        TEXT,
                symbol            TEXT NOT NULL,
                direction         TEXT NOT NULL DEFAULT 'LONG',
                signal_strength   INTEGER DEFAULT 0,
                signal_confidence REAL DEFAULT 0.0,
                created_at        TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 幂等 ALTER: 把扩展字段加到已存在的 (旧最小) 表上
        for col_def in [
            # 兼容: 旧最小 schema 可能没有 created_at (SQLite ALTER 不支持 CURRENT_TIMESTAMP DEFAULT, INSERT 处需自填)
            ("created_at",      "ALTER TABLE trade_attribution ADD COLUMN created_at TEXT"),
            # Phase A (2026-05-14): 归属策略
            ("owner_strategy",  "ALTER TABLE trade_attribution ADD COLUMN owner_strategy TEXT"),
            # 入场快照
            ("decision_id",     "ALTER TABLE trade_attribution ADD COLUMN decision_id TEXT"),
            ("entry_time",      "ALTER TABLE trade_attribution ADD COLUMN entry_time TEXT"),
            ("entry_price",     "ALTER TABLE trade_attribution ADD COLUMN entry_price REAL"),
            ("quantity",        "ALTER TABLE trade_attribution ADD COLUMN quantity INTEGER"),
            # 入场归因 (5 大因子 + thesis)
            ("entry_thesis",    "ALTER TABLE trade_attribution ADD COLUMN entry_thesis TEXT"),
            ("factor_trend",    "ALTER TABLE trade_attribution ADD COLUMN factor_trend REAL"),
            ("factor_momentum", "ALTER TABLE trade_attribution ADD COLUMN factor_momentum REAL"),
            ("factor_volume",   "ALTER TABLE trade_attribution ADD COLUMN factor_volume REAL"),
            ("factor_orderflow","ALTER TABLE trade_attribution ADD COLUMN factor_orderflow REAL"),
            ("factor_lifecycle","ALTER TABLE trade_attribution ADD COLUMN factor_lifecycle REAL"),
            # 退出快照
            ("exit_time",       "ALTER TABLE trade_attribution ADD COLUMN exit_time TEXT"),
            ("exit_price",      "ALTER TABLE trade_attribution ADD COLUMN exit_price REAL"),
            ("realized_pnl",    "ALTER TABLE trade_attribution ADD COLUMN realized_pnl REAL"),
            ("exit_reason",     "ALTER TABLE trade_attribution ADD COLUMN exit_reason TEXT"),
            ("exit_quality",    "ALTER TABLE trade_attribution ADD COLUMN exit_quality TEXT"),
            ("holding_minutes", "ALTER TABLE trade_attribution ADD COLUMN holding_minutes INTEGER"),
            # 上下文
            ("session",         "ALTER TABLE trade_attribution ADD COLUMN session TEXT"),
            ("vix_at_entry",    "ALTER TABLE trade_attribution ADD COLUMN vix_at_entry REAL"),
            ("vix_at_exit",     "ALTER TABLE trade_attribution ADD COLUMN vix_at_exit REAL"),
            ("regime",          "ALTER TABLE trade_attribution ADD COLUMN regime TEXT"),
            # 扩展
            ("metadata_json",   "ALTER TABLE trade_attribution ADD COLUMN metadata_json TEXT"),
            ("updated_at",      "ALTER TABLE trade_attribution ADD COLUMN updated_at TEXT"),
        ]:
            try:
                conn.execute(col_def[1])
                logger.debug(f"[TradeStore.attribution] 新增列: {col_def[0]}")
            except Exception:
                pass  # 列已存在

        # 3 个索引: symbol/decision_id/entry_time
        for idx_def in [
            "CREATE INDEX IF NOT EXISTS idx_attr_symbol ON trade_attribution(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_attr_decision ON trade_attribution(decision_id)",
            "CREATE INDEX IF NOT EXISTS idx_attr_trace ON trade_attribution(trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_attr_entry_time ON trade_attribution(entry_time)",
            "CREATE INDEX IF NOT EXISTS idx_attr_trade_date ON trade_attribution(trade_date)",
            "CREATE INDEX IF NOT EXISTS idx_attr_owner ON trade_attribution(owner_strategy)",
        ]:
            try:
                conn.execute(idx_def)
            except Exception:
                pass

    def save_trade(self, record: Dict[str, Any]) -> int:
        """
        保存一笔完整交易记录

        Args:
            record: dict，包含交易信息和投票详情
                新增支持:
                  - source_type: 来源分类 (MAIN_PIPELINE/SCALPING/LLM_PLAN/LLM_RECOMMEND)
                  - entry_reason: 下单理由 (JSON 数组或字符串)
                  - signal_details: 信号详情 (JSON 对象)
                  - metadata_json: 扩展元数据 (JSON 对象)
        
        Returns:
            新记录的 rowid
        """
        import json
        import logging as _logging
        _logger = _logging.getLogger(__name__)
        
        # P0 数据质量守卫 (2026-05-22): 关键归因字段完整性检查
        _symbol = record.get("symbol", "")
        if not _symbol:
            _logger.error(f"[数据质量守卫] save_trade 缺少 symbol! record keys: {list(record.keys())}")
        _missing_fields = []
        if not record.get("strategy_id"):
            _missing_fields.append("strategy_id")
        if not record.get("exit_reason"):
            _missing_fields.append("exit_reason")
        if record.get("signal_strength") in (None, 0, ""):
            _missing_fields.append("signal_strength")
        if _missing_fields:
            _logger.warning(
                f"[数据质量守卫] {_symbol} save_trade 缺少归因字段: {_missing_fields} | "
                f"source={record.get('source_type', 'UNKNOWN')} | "
                f"trace_id={record.get('trace_id', 'NONE')}"
            )
        
        with self._lock:
            conn = self._get_conn()
            try:
                # 处理 entry_reason (支持 list 和 str)
                entry_reason = record.get("entry_reason")
                if isinstance(entry_reason, list):
                    entry_reason = json.dumps(entry_reason, ensure_ascii=False)
                elif not entry_reason:
                    entry_reason = None

                # 处理 signal_details
                signal_details = record.get("signal_details")
                if isinstance(signal_details, dict):
                    signal_details = json.dumps(signal_details, ensure_ascii=False)
                elif not signal_details:
                    signal_details = None

                # 处理 metadata_json
                metadata_json = record.get("metadata_json")
                if isinstance(metadata_json, dict):
                    metadata_json = json.dumps(metadata_json, ensure_ascii=False)
                elif not metadata_json:
                    metadata_json = None

                cursor = conn.execute("""
                    INSERT INTO trade_records (
                        trade_date, symbol, direction, entry_time, exit_time,
                        entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                        exit_reason, holding_hours, signal_strength, signal_confidence,
                        futu_order_id, close_futu_order_id, voting_result, final_strength, consensus_level,
                        model_scores, strategy_id, decision_reason, max_drawdown, trace_id,
                        source_type, entry_reason, signal_details, metadata_json,
                        owner_strategy, mode, market_regime,
                        created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    record.get("trade_date", ""),
                    record.get("symbol", ""),
                    record.get("direction", ""),
                    record.get("entry_time", ""),
                    record.get("exit_time", ""),
                    record.get("entry_price", 0.0),
                    record.get("exit_price", 0.0),
                    record.get("quantity", 0),
                    record.get("realized_pnl", 0.0),
                    record.get("pnl_pct", 0.0),
                    record.get("exit_reason", ""),
                    record.get("holding_hours", 0.0),
                    record.get("signal_strength", 0),
                    record.get("signal_confidence", 0.0),
                    record.get("futu_order_id", ""),
                    record.get("close_futu_order_id", ""),
                    json.dumps(record.get("voting_result", {})) if record.get("voting_result") else None,
                    record.get("final_strength", 0),
                    record.get("consensus_level", 0.0),
                    json.dumps(record.get("model_scores", [])) if record.get("model_scores") else None,
                    record.get("strategy_id", "voting_llm"),
                    record.get("decision_reason", ""),
                    record.get("max_drawdown", 0.0),
                    record.get("trace_id", ""),
                    record.get("source_type", "MAIN_PIPELINE"),
                    entry_reason,
                    signal_details,
                    metadata_json,
                    # Phase A (2026-05-14): owner_strategy 必传 — 默认 main
                    record.get("owner_strategy") or "main",
                    # 沙盒模式 (2026-05-14): mode 字段, 默认 normal
                    record.get("mode") or _get_runtime_mode(),
                    # O4 (2026-05-16): market_regime 显式落库 (Y1 RegimeDetector 数据可用)
                    record.get("market_regime") or "UNKNOWN",
                    datetime.now().isoformat(),
                ))
                conn.commit()
                rowid = cursor.lastrowid
                logger.info(
                    f"[TradeStore] 保存交易记录 id={rowid} "
                    f"{record.get('symbol')} {record.get('direction')} "
                    f"pnl=${record.get('realized_pnl', 0):.2f} "
                    f"strength={record.get('final_strength', 0)}"
                )
                return rowid
            finally:
                conn.close()

    def query_by_date(self, trade_date: str) -> List[Dict[str, Any]]:
        """
        按交易日期查询记录

        Args:
            trade_date: 'YYYY-MM-DD'
        """
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                           entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                           exit_reason, holding_hours, signal_strength, signal_confidence,
                           futu_order_id, created_at
                    FROM trade_records
                    WHERE trade_date = ?
                    ORDER BY entry_time ASC
                """, (trade_date,)).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def query_today(self) -> List[Dict[str, Any]]:
        """查询今日交易记录"""
        today = date.today().strftime("%Y-%m-%d")
        return self.query_by_date(today)

    def query_range(self, start: str, end: str) -> List[Dict[str, Any]]:
        """
        查询日期区间内的交易记录

        Args:
            start: 'YYYY-MM-DD'
            end:   'YYYY-MM-DD'
        """
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                           entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                           exit_reason, holding_hours, signal_strength, signal_confidence,
                           futu_order_id, created_at
                    FROM trade_records
                    WHERE trade_date BETWEEN ? AND ?
                    ORDER BY entry_time ASC
                """, (start, end)).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def stats_by_date(self, trade_date: str) -> Dict[str, Any]:
        """
        统计指定日期的交易绩效

        Returns:
            {
                total_trades, wins, losses, win_rate,
                total_pnl, avg_pnl, max_pnl, min_pnl,
                avg_holding_hours, avg_strength, avg_confidence,
                exit_reason_dist: {reason: count},
            }
        """
        records = self.query_by_date(trade_date)
        if not records:
            return {"total_trades": 0, "total_pnl": 0.0}

        total_pnl = sum(r["realized_pnl"] for r in records)
        wins = [r for r in records if r["realized_pnl"] > 0]
        losses = [r for r in records if r["realized_pnl"] <= 0]

        exit_dist: Dict[str, int] = {}
        for r in records:
            reason = r.get("exit_reason", "UNKNOWN")
            exit_dist[reason] = exit_dist.get(reason, 0) + 1

        total = len(records)
        return {
            "total_trades":     total,
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate":         len(wins) / total if total else 0.0,
            "total_pnl":        total_pnl,
            "avg_pnl":          total_pnl / total if total else 0.0,
            "max_pnl":          max(r["realized_pnl"] for r in records),
            "min_pnl":          min(r["realized_pnl"] for r in records),
            "avg_holding_hours": sum(r["holding_hours"] for r in records) / total,
            "avg_strength":     sum(r["signal_strength"] for r in records) / total,
            "avg_confidence":   sum(r["signal_confidence"] for r in records) / total,
            "exit_reason_dist": exit_dist,
            "records":          records,
        }

    def stats_today(self) -> Dict[str, Any]:
        """统计今日交易绩效"""
        return self.stats_by_date(date.today().strftime("%Y-%m-%d"))

    # ------------------------------------------------------------------
    # 未平仓持仓快照（重启后恢复用）
    # ------------------------------------------------------------------

    def upsert_open_position(self, position, source: str = "scheduler", trace_id: str = "",
                              action: str = "", is_filled: bool = True,
                              is_partially_reduced: bool = False) -> None:
        """写入 / 更新未平仓持仓快照 (统一仓位状态真相源)

        Args:
            position: Position 对象
            source: 写入来源 (scheduler/exit_monitor/sync)
            trace_id: 全链路归因 ID (默认从 position.trace_id 提取)
            action: 触发写入的动作描述 (open/close/reduce/sync)
            is_filled: 是否已成交
            is_partially_reduced: 是否已部分减仓
        """
        import json
        # P0 Fix (2026-05-02): Auto-extract trace_id from position if not provided
        if not trace_id:
            trace_id = getattr(position, "trace_id", "") or ""
        with self._lock:
            conn = self._get_conn()
            try:
                # 先读取当前版本号和 is_anomalous 标记 (乐观锁)
                row = conn.execute(
                    "SELECT version, is_anomalous FROM open_positions WHERE symbol = ?",
                    (position.symbol,)
                ).fetchone()
                current_version = (row[0] if row else 0) + 1
                # P0 修复 (2026-05-12): 防止 update_trail 等操作覆盖 mark_anomalous 设置的 is_anomalous=1
                # 如果 DB 中已标记异常且当前 position 未显式设置 _close_blocked，保留原值
                existing_is_anomalous = bool(row[1]) if (row and len(row) > 1) else False
                has_close_blocked = (getattr(position, "metadata", {}) or {}).get("_close_blocked", False)
                preserve_anomalous = existing_is_anomalous and not has_close_blocked

                # 构建动作日志
                action_entry = {
                    "time": datetime.now().isoformat(),
                    "action": action or "upsert",
                    "source": source,
                    "trace_id": trace_id,
                    "version": current_version,
                }

                # 如果已有记录，追加动作日志
                if row:
                    existing_log = conn.execute(
                        "SELECT action_log_json FROM open_positions WHERE symbol = ?",
                        (position.symbol,)
                    ).fetchone()
                    if existing_log and existing_log[0]:
                        try:
                            log_list = json.loads(existing_log[0])
                            log_list.append(action_entry)
                            log_list = log_list[-10:]  # 只保留最近 10 条
                            action_log_json = json.dumps(log_list)
                        except json.JSONDecodeError:
                            action_log_json = json.dumps([action_entry])
                    else:
                        action_log_json = json.dumps([action_entry])
                else:
                    action_log_json = json.dumps([action_entry])

                conn.execute("""
                    INSERT OR REPLACE INTO open_positions (
                        symbol, direction, size, avg_entry_price,
                        initial_sl, current_sl, initial_tp,
                        take_profit_json, trail_state_json,
                        entry_time, time_stop_hours,
                        strategy_id, signal_id, position_id, market_regime,
                        version, source, sync_status, last_sync_at,
                        is_filled, is_partially_reduced, is_anomalous,
                        trace_id, action_log_json, owner_strategy, mode,
                        updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    position.symbol,
                    str(position.direction.value) if hasattr(position.direction, "value") else str(position.direction),
                    position.size,
                    position.avg_entry_price,
                    position.initial_sl,
                    position.current_sl,
                    position.initial_tp,
                    json.dumps(position.take_profit_levels or []),
                    json.dumps(position.trail_state or {}),
                    position.entry_time.isoformat() if position.entry_time else "",
                    position.time_stop_hours,
                    getattr(position, "strategy_id", ""),
                    getattr(position, "signal_id", ""),
                    getattr(position, "position_id", ""),
                    str(position.market_regime.value) if hasattr(position.market_regime, "value") else "",
                    current_version,
                    source,
                    "synced",
                    datetime.now().isoformat(),
                    int(is_filled),
                    int(is_partially_reduced),
                    int(preserve_anomalous or (getattr(position, "metadata", {}) or {}).get("_close_blocked", False)),
                    trace_id,
                    action_log_json,
                    # Phase A: 写 owner_strategy
                    getattr(position, "owner_strategy", None) or "main",
                    _get_runtime_mode(),
                    datetime.now().isoformat(),
                ))
                conn.commit()
                logger.info(
                    f"[TruthSource] 仓位写入 {position.symbol} "
                    f"v{current_version} src={source} action={action}"
                )
            finally:
                conn.close()

    def remove_open_position(self, symbol: str, source: str = "scheduler",
                              trace_id: str = "", exit_reason: str = "") -> None:
        """平仓后标记持仓快照 (软删除, 2026-04-24 方案A)

        方案A: 股票退出由 Daemon 独占, 主线程可能直接调用此方法的来源:
            - 期权平仓 (scheduler)
            - 主线程主动平仓 (scheduler, 仅特殊分支)
            - 外部同步脚本 (sync/emergency)

        统一软删除语义: UPDATE sync_status='closed_by_scheduler', 与 Daemon 的
        'closed_by_daemon' 形成对称. auto_position_reconcile.py 统一延后 DELETE.
        """
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT symbol, sync_status FROM open_positions WHERE symbol = ?",
                    (symbol,)
                ).fetchone()
                if not row:
                    return
                # 根据 source 区分软删除标签, 便于审计
                new_status = {
                    "scheduler": "closed_by_scheduler",
                    "sync":      "closed_by_sync",
                    "emergency": "closed_emergency",
                }.get(source, "closed_by_scheduler")
                conn.execute("""
                    UPDATE open_positions
                    SET sync_status=?, updated_at=?
                    WHERE symbol=?
                """, (new_status, datetime.now().isoformat(), symbol))
                conn.commit()
                logger.info(
                    f"[TruthSource] 仓位软删除 {symbol} → {new_status} "
                    f"src={source} reason={exit_reason} trace={trace_id}"
                )
            finally:
                conn.close()

    def load_open_positions(self) -> List[Dict[str, Any]]:
        """
        加载所有未平仓持仓快照（服务重启后恢复用）
        Returns: list of dict，可用于重建 Position 对象

        P0-1 升级: 包含真相源字段 (version, source, sync_status, trace_id, action_log)
        """
        import json
        with self._lock:
            conn = self._get_conn()
            try:
                # P0 修复 (2026-04-24 方案A): 过滤所有软删除/失联状态,
                # 避免主线程启动恢复时把已关闭仓位重新载入内存
                rows = conn.execute("""
                    SELECT symbol, direction, size, avg_entry_price,
                           initial_sl, current_sl, initial_tp,
                           take_profit_json, trail_state_json,
                           entry_time, time_stop_hours,
                           strategy_id, signal_id, position_id, market_regime,
                           version, source, sync_status, trace_id,
                           is_filled, is_partially_reduced, is_anomalous,
                           action_log_json, last_sync_at
                    FROM open_positions
                    WHERE (sync_status IS NULL
                           OR sync_status NOT IN (
                                'closed_by_daemon', 'closed_by_scheduler',
                                'closed_by_sync', 'closed_emergency',
                                'stale_not_in_broker'))
                      AND (is_filled IS NULL OR is_filled = 1)
                """).fetchall()
                result = []
                for r in rows:
                    result.append({
                        "symbol":            r[0],
                        "direction":         r[1],
                        "size":              r[2],
                        "avg_entry_price":   r[3],
                        "initial_sl":        r[4],
                        "current_sl":        r[5],
                        "initial_tp":        r[6],
                        "take_profit_levels": json.loads(r[7] or "[]"),
                        "trail_state":       json.loads(r[8] or "{}"),
                        "entry_time":        r[9],
                        "time_stop_hours":   r[10] or 72,
                        "strategy_id":       r[11],
                        "signal_id":         r[12],
                        "position_id":       r[13],
                        "market_regime":     r[14],
                        # P0-1 真相源字段
                        "version":           r[15] if len(r) > 15 else 1,
                        "source":            r[16] if len(r) > 16 else "scheduler",
                        "sync_status":       r[17] if len(r) > 17 else "synced",
                        "trace_id":          r[18] if len(r) > 18 else "",
                        "is_filled":         bool(r[19]) if len(r) > 19 else True,
                        "is_partially_reduced": bool(r[20]) if len(r) > 20 else False,
                        "is_anomalous":      bool(r[21]) if len(r) > 21 else False,
                        "action_log":        json.loads(r[22]) if (len(r) > 22 and r[22]) else [],
                        "last_sync_at":      r[23] if len(r) > 23 else "",
                    })
                return result
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 仓位状态真相源操作 (P0-1 新增)
    # ------------------------------------------------------------------

    def get_position_truth(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取单个仓位的真相源状态 (统一读取入口)

        Returns:
            仓位状态 dict，包含 version/source/sync_status 等字段
            如果仓位不存在返回 None
        """
        import json
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("""
                    SELECT symbol, direction, size, avg_entry_price,
                           current_sl, initial_tp, take_profit_json, trail_state_json,
                           version, source, sync_status, trace_id,
                           is_filled, is_partially_reduced, is_anomalous,
                           action_log_json, last_sync_at, entry_time
                    FROM open_positions WHERE symbol = ?
                """, (symbol,)).fetchone()
                if not row:
                    return None
                return {
                    "symbol":               row[0],
                    "direction":            row[1],
                    "size":                 row[2],
                    "avg_entry_price":      row[3],
                    "current_sl":           row[4],
                    "initial_tp":           row[5],
                    "take_profit_levels":   json.loads(row[6] or "[]"),
                    "trail_state":          json.loads(row[7] or "{}"),
                    "version":              row[8],
                    "source":               row[9],
                    "sync_status":          row[10],
                    "trace_id":             row[11],
                    "is_filled":            bool(row[12]),
                    "is_partially_reduced": bool(row[13]),
                    "is_anomalous":         bool(row[14]),
                    "action_log":           json.loads(row[15]) if row[15] else [],
                    "last_sync_at":         row[16],
                    "entry_time":           row[17],
                }
            finally:
                conn.close()

    def mark_sync_pending(self, symbol: str, trace_id: str = "") -> None:
        """标记仓位为待同步状态"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    UPDATE open_positions
                    SET sync_status = 'pending_sync', last_sync_at = ?, trace_id = ?
                    WHERE symbol = ?
                """, (datetime.now().isoformat(), trace_id, symbol))
                conn.commit()
            finally:
                conn.close()

    def mark_sync_complete(self, symbol: str) -> None:
        """标记仓位同步完成"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    UPDATE open_positions
                    SET sync_status = 'synced', last_sync_at = ?
                    WHERE symbol = ?
                """, (datetime.now().isoformat(), symbol))
                conn.commit()
            finally:
                conn.close()

    def mark_anomalous(self, symbol: str, reason: str = "") -> None:
        """标记仓位为异常状态"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    UPDATE open_positions
                    SET is_anomalous = 1, sync_status = 'error', last_sync_at = ?
                    WHERE symbol = ?
                """, (datetime.now().isoformat(), symbol))
                logger.warning(f"[TruthSource] 仓位标记异常 {symbol}: {reason}")
                conn.commit()
            finally:
                conn.close()

    def unmark_anomalous(self, symbol: str, reason: str = "") -> bool:
        """清除异常标记，恢复正常监控 (P0 fix 2026-05-13: QQQ 自动恢复)

        当 Broker 确认持仓存在且 DB 数据一致时，自动清除 is_anomalous 标记，
        使退出监控逻辑恢复正常。

        Args:
            symbol: 标的代码 (如 'US.QQQ')
            reason: 恢复原因 (日志记录)

        Returns:
            是否成功清除
        """
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT is_anomalous, version FROM open_positions WHERE symbol = ?",
                    (symbol,)
                ).fetchone()
                if not row:
                    logger.debug(f"[TruthSource] 未找到 {symbol} 持仓记录，无法清除异常标记")
                    return False
                if not row[0]:
                    logger.debug(f"[TruthSource] {symbol} 已经是正常状态 (is_anomalous=0)")
                    return False
                new_version = row[1] + 1
                conn.execute("""
                    UPDATE open_positions
                    SET is_anomalous = 0, sync_status = 'synced',
                        version = ?, last_sync_at = ?
                    WHERE symbol = ?
                """, (new_version, datetime.now().isoformat(), symbol))
                conn.commit()
                logger.info(f"[TruthSource] 已恢复 {symbol} 正常监控 (v{row[1]}→v{new_version}): {reason}")
                return True
            except Exception as e:
                logger.error(f"[TruthSource] 清除 {symbol} 异常标记失败: {e}")
                return False
            finally:
                conn.close()

    def update_position_field(self, symbol: str, field: str, value: Any,
                               source: str = "scheduler", trace_id: str = "",
                               action: str = "") -> bool:
        """
        安全更新单个仓位字段 (带版本乐观锁)

        Args:
            symbol: 标的代码
            field: 要更新的字段名
            value: 新值
            source: 写入来源
            trace_id: 归因 ID
            action: 动作描述

        Returns:
            是否更新成功
        """
        with self._lock:
            conn = self._get_conn()
            try:
                # 读取当前版本
                row = conn.execute(
                    "SELECT version FROM open_positions WHERE symbol = ?",
                    (symbol,)
                ).fetchone()
                if not row:
                    return False
                new_version = row[0] + 1

                # 构建动作日志
                action_entry = json.dumps([{
                    "time": datetime.now().isoformat(),
                    "action": action or f"update_{field}",
                    "source": source,
                    "trace_id": trace_id,
                    "version": new_version,
                }])

                conn.execute(f"""
                    UPDATE open_positions
                    SET {field} = ?, version = ?, source = ?, sync_status = 'synced',
                        last_sync_at = ?, trace_id = ?,
                        action_log_json = CASE
                            WHEN action_log_json IS NULL THEN ?
                            ELSE json_insert(action_log_json, '$[' || json_array_length(action_log_json) || ']', json(?))
                        END
                    WHERE symbol = ?
                """, (value, new_version, source, datetime.now().isoformat(), trace_id,
                      action_entry, action_entry, symbol))
                conn.commit()
                return True
            finally:
                conn.close()

    def get_pending_sync_positions(self, max_age_seconds: int = 300) -> List[str]:
        """获取超过指定时间仍未同步的仓位 (用于告警)"""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT symbol FROM open_positions
                    WHERE sync_status = 'pending_sync'
                    AND datetime(last_sync_at) < datetime('now', '-{max_age_seconds} seconds')
                """.replace("{max_age_seconds}", str(max_age_seconds))).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 期权持仓（SHORT 空头代理）
    # ------------------------------------------------------------------

    def upsert_option_position(self, record: Dict[str, Any]) -> None:
        """
        写入 / 更新期权持仓快照。

        record 必填字段：
            symbol, option_code, qty, entry_price, entry_time
        可选字段：
            strike_price, expiry_date, cost_total,
            signal_strength, signal_confidence, is_simulated
        """
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO option_positions (
                        symbol, option_code, direction, qty,
                        entry_price, strike_price, expiry_date,
                        entry_time, cost_total,
                        signal_strength, signal_confidence,
                        is_simulated, mode, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    record["symbol"],
                    record["option_code"],
                    record.get("direction", "SHORT_PROXY"),
                    record["qty"],
                    record["entry_price"],
                    record.get("strike_price", 0.0),
                    record.get("expiry_date", ""),
                    record.get("entry_time", datetime.now().isoformat()),
                    record.get("cost_total", record["qty"] * record["entry_price"] * 100),
                    record.get("signal_strength", 0),
                    record.get("signal_confidence", 0.0),
                    int(record.get("is_simulated", False)),
                    _get_runtime_mode(),
                    datetime.now().isoformat(),
                ))
                conn.commit()
                logger.info(
                    f"[TradeStore] 期权持仓写入 {record['symbol']} "
                    f"{record['option_code']} qty={record['qty']} "
                    f"entry=${record['entry_price']:.2f}"
                )
            finally:
                conn.close()

    def remove_option_position(self, option_code: str) -> None:
        """平仓后删除期权持仓记录"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM option_positions WHERE option_code = ?",
                    (option_code,)
                )
                conn.commit()
                logger.info(f"[TradeStore] 期权持仓删除 {option_code}")
            finally:
                conn.close()

    def load_option_positions(self) -> List[Dict[str, Any]]:
        """加载所有未平仓期权持仓"""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT symbol, option_code, direction, qty,
                           entry_price, strike_price, expiry_date,
                           entry_time, cost_total,
                           signal_strength, signal_confidence, is_simulated
                    FROM option_positions
                    ORDER BY entry_time ASC
                """).fetchall()
                return [
                    {
                        "symbol":             r[0],
                        "option_code":        r[1],
                        "direction":          r[2],
                        "qty":                r[3],
                        "entry_price":        r[4],
                        "strike_price":       r[5],
                        "expiry_date":        r[6],
                        "entry_time":         r[7],
                        "cost_total":         r[8],
                        "signal_strength":    r[9],
                        "signal_confidence":  r[10],
                        "is_simulated":       bool(r[11]),
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        return {
            "id":                row[0],
            "trade_date":        row[1],
            "symbol":            row[2],
            "direction":         row[3],
            "entry_time":        row[4],
            "exit_time":         row[5],
            "entry_price":       row[6],
            "exit_price":        row[7],
            "quantity":          row[8],
            "realized_pnl":      row[9],
            "pnl_pct":           row[10],
            "exit_reason":       row[11],
            "holding_hours":     row[12],
            "signal_strength":   row[13],
            "signal_confidence": row[14],
            "futu_order_id":     row[15],
            "created_at":        row[16],
        }
