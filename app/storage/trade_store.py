"""
交易记录持久化存储 — SSOT (Single Source of Truth)

⚠️ 这是 trade_records + open_positions 的唯一权威写入方.
   所有交易管线 (主策略/ORB/手动/reconciler) 必须通过此模块写入.

架构说明:
  - TradeStore (本文件): ftseek_us.db — 交易 + 持仓 SSOT
  - TradeRecorder:       trade_records.db — 评审系统专用 (独立, 有心理学字段)

将每笔完整交易（开仓→平仓）写入 SQLite trade_records 表，
重启后历史不丢失，支持日报复盘查询。
"""
import sqlite3
import threading
import logging
import json
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path

from app.core.trade_identity import ensure_test_symbol_not_in_production_db
from app.risk.exit_reasons import normalize_exit_reason
from app.utils.timezone import _et_trade_date, now_et, CST, _localize

logger = logging.getLogger(__name__)


def _parse_sync_ts(raw: Optional[str]) -> Optional[datetime]:
    """把 open_positions.last_sync_at 解析成 aware datetime; 解析不出返回 None。

    兼容两种格式 (该列 DDL 无默认值 ⇒ 不存在 SQLite `datetime('now')` 那条写入路):
      ① 带偏移 isoformat — **现役格式**: 该列全部 **6 个**写方已于 2026-09-05 M-6/B1 第三阶
         统一改用 now_et() (upsert_open_position / mark_sync_pending / mark_sync_complete /
         mark_anomalous / unmark_anomalous / update_position_field);
      ② naive isoformat — 历史行 / 手工 SQL 改库 / 别名兜底路径遗留 = 服务器本地(CST)墙钟。
    naive 一律按 **CST** 解释 = 与**旧**写入方同一把尺子 (跨尺比较正是 D-06 的病灶)。
    该 naive 分支**刻意保留为兜底, 不因写方已统一而删**: 现库 3 行 last_sync_at 全为 NULL
    (活行由 upsert_position_simple 写, 其列清单不含本列), 既无历史 naive 行需归一, 也无法保证
    今后不出现 (回滚 / 手工修库 / 别名 `_dt` 兜底) ⇒ 保留成本 3 行, 收益是永不因格式意外静默漏报。
    """
    if not raw:
        return None
    _text = str(raw).strip()
    if not _text:
        return None
    try:
        _dt = datetime.fromisoformat(_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if _dt.tzinfo is None:
        _dt = _localize(_dt, CST)
    return _dt


def assign_cohort(symbol: str, trade_date: str) -> str:
    """A/B cohort 分配 (RFC-061). 确定性: 同 symbol+date 必同组, 幂等可重算.

    用于"盈利 shadow 仓位过夜持有到 T+5"实验: cohort B 跳过 EOD 强平,
    cohort A 维持现有日内行为 (对照). 无 trade_date 时落 A 组 (保守, 不改变行为).
    """
    if not symbol or not trade_date:
        return "A"
    return "B" if (hash(f"{symbol}-{trade_date}") % 2 == 1) else "A"


def _get_runtime_mode() -> str:
    """运行时取 mode (sandbox / normal). 失败默认 normal."""
    try:
        from app.core.config_container import get_config
        if get_config().get_dotted("mode.sandbox.enabled", False):
            return "sandbox"
    except Exception:
        pass
    return "normal"


def _get_trade_mode() -> str:
    """2026-07-03: 根据 AUTO_TRADE_ACCOUNT 环境变量返回 simulate/live.

    约定:
      - AUTO_TRADE_ACCOUNT=sim (默认) -> 'simulate'
      - AUTO_TRADE_ACCOUNT=real       -> 'live'
      - 解析异常                      -> 'simulate' (最安全兑底)
    """
    try:
        import os as _os
        _auto_acc = _os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        return "live" if _auto_acc == "real" else "simulate"
    except Exception:
        return "simulate"


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

    # M11 (2026-06-03): 单例保护 — 防止多实例创建导致锁失效
    _instances: Dict[str, 'TradeStore'] = {}

    def __new__(cls, db_path: str):
        if db_path not in cls._instances:
            cls._instances[db_path] = super().__new__(cls)
        return cls._instances[db_path]

    def __init__(self, db_path: str):
        if hasattr(self, '_initialized') and self._initialized:
            return  # 已初始化, 跳过
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
        self._initialized = True

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
                        fees_est          REAL,
                        pnl_net           REAL,
                        fees_source       TEXT,
                        net_pnl_pct       REAL,
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

                # 2026-10-09 (DEB-20261009·test_legacy_db): futu_order_id 在
                # CREATE TABLE DDL 里但没有 ALTER TABLE 兜底 — 早于该列加入的
                # 遗留 DB (CREATE TABLE IF NOT EXISTS 是 no-op) 走到下方迁移
                # UPDATE (tr.futu_order_id) 或 save_trade INSERT 时
                # OperationalError. 补 ALTER 使 _init_db 对任意历史 schema 幂等.
                try:
                    conn.execute("ALTER TABLE trade_records ADD COLUMN futu_order_id TEXT")
                except Exception as _ddl_e:
                    # 2026-10-28 except:pass 清剿: 绝大多数是"列已存在", 但若是锁/权限类
                    # 失败则下游 INSERT 才会炸 — 留痕以区分两者。
                    logger.info("[trade_store] futu_order_id 迁移跳过 (多为列已存在): %s", _ddl_e)

                # Karpathy 统一记录: 添加来源分类 + 理由字段 (2026-04-29)
                # Phase A (2026-05-14): 加 owner_strategy 列, 与 source_type 互补
                #   - source_type: 来源大类 (MAIN_PIPELINE/...)
                #   - owner_strategy: 具体 strategy.name (main/fast_lane/llm_plan)
                for col_def in [
                    ("fees_est", "ALTER TABLE trade_records ADD COLUMN fees_est REAL"),
                    ("pnl_net", "ALTER TABLE trade_records ADD COLUMN pnl_net REAL"),
                    ("fees_source", "ALTER TABLE trade_records ADD COLUMN fees_source TEXT"),
                    ("net_pnl_pct", "ALTER TABLE trade_records ADD COLUMN net_pnl_pct REAL"),
                    ("source_type", "ALTER TABLE trade_records ADD COLUMN source_type TEXT DEFAULT 'MAIN_PIPELINE'"),
                    ("entry_reason", "ALTER TABLE trade_records ADD COLUMN entry_reason TEXT"),
                    ("signal_details", "ALTER TABLE trade_records ADD COLUMN signal_details TEXT"),
                    ("metadata_json", "ALTER TABLE trade_records ADD COLUMN metadata_json TEXT"),
                    ("owner_strategy", "ALTER TABLE trade_records ADD COLUMN owner_strategy TEXT"),
                    # 沙盒模式标记 (2026-05-14): normal / sandbox
                    ("mode", "ALTER TABLE trade_records ADD COLUMN mode TEXT DEFAULT 'normal'"),
                    # O4 (2026-05-16): regime 显式落库 (Y1 RegimeDetector 数据复盘可见)
                    ("market_regime", "ALTER TABLE trade_records ADD COLUMN market_regime TEXT DEFAULT 'UNKNOWN'"),
                    # Phase 1 (2026-05-31): 模拟/实盘隔离 — 根据 TRADE_ENV 填充, 默认 simulate
                    ("trade_mode", "ALTER TABLE trade_records ADD COLUMN trade_mode TEXT DEFAULT 'simulate'"),
                    # C3 (2026-06-03): 信号归因链路 — signal_id + decision_id 必须存在
                    ("signal_id", "ALTER TABLE trade_records ADD COLUMN signal_id TEXT DEFAULT ''"),
                    ("decision_id", "ALTER TABLE trade_records ADD COLUMN decision_id TEXT DEFAULT ''"),
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
                    "CREATE INDEX IF NOT EXISTS idx_trade_records_trade_mode ON trade_records(trade_mode)",
                    "CREATE INDEX IF NOT EXISTS idx_trade_records_decision_id "
                    "ON trade_records(decision_id)",
                ]:
                    try:
                        conn.execute(idx_def)
                    except Exception:
                        pass

                # 2026-07-01: option_positions 表已删除 (自动交易不做期权)

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
                        entry_broker_order_id TEXT,
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

                # ── Daemon 心跳表退役 (2026-08-27): 表 2026-04-24 建立后全仓
                #    零写入零读取 (0 行, grep 实证), 运行时心跳实际走
                #    app/observability/intraday_heartbeat (钉钉推送链) — 死表
                #    只会伪装"有监控". 幂等 DROP. 回滚: 恢复原 CREATE 块. ──
                try:
                    conn.execute("DROP TABLE IF EXISTS daemon_heartbeat")
                except Exception:
                    pass

                # F19 (2026-05-12): 归因表完整 schema
                self._ensure_trade_attribution_schema(conn)

                # ── entry_context_snapshot 死表退役 (2026-09-13, 用户裁"删除"): 表
                #    2026-05-31 建立后全仓零行落库 (save_context_snapshot 零调用方,
                #    watchdog D1:never_fed 实锤)。入场时刻真数值指标快照已由 F2
                #    (DEB-20260804) 落 trade_records.factor_detail.ts_ctx (8月起
                #    275/275=100% 覆盖), 本表功能被活通道完全取代。唯一读者
                #    AttributionScheduler 本身是死码 (零消费方, T5 钩子)。幂等 DROP。
                #    回滚: 恢复原 CREATE 块 (git revert 本提交)。 ──
                try:
                    conn.execute("DROP TABLE IF EXISTS entry_context_snapshot")
                except Exception:
                    pass

                # Phase 2 (2026-05-31): exit_reason 标准化术语表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS exit_reason_glossary (
                        reason_code        TEXT PRIMARY KEY,
                        category           TEXT NOT NULL,
                        description       TEXT,
                        total_count        INTEGER DEFAULT 0,
                        win_count          INTEGER DEFAULT 0,
                        avg_pnl            REAL DEFAULT 0,
                        avg_holding_hours  REAL DEFAULT 0,
                        last_updated       TEXT DEFAULT (datetime('now'))
                    )
                """)

                # P0 (2026-06-23): 因子归因列 — save_trade INSERT 引用但从未 ALTER ADD
                for col_def in [
                    ("factor_detail", "ALTER TABLE trade_records ADD COLUMN factor_detail TEXT"),
                    ("f1_trend",      "ALTER TABLE trade_records ADD COLUMN f1_trend REAL DEFAULT 0"),
                    ("f2_momentum",   "ALTER TABLE trade_records ADD COLUMN f2_momentum REAL DEFAULT 0"),
                    ("f3_volume",     "ALTER TABLE trade_records ADD COLUMN f3_volume REAL DEFAULT 0"),
                    ("f4_orderflow",  "ALTER TABLE trade_records ADD COLUMN f4_orderflow REAL DEFAULT 0"),
                    ("f5_lifecycle",  "ALTER TABLE trade_records ADD COLUMN f5_lifecycle REAL DEFAULT 0"),
                ]:
                    try:
                        conn.execute(col_def[1])
                        logger.info(f"[TradeStore] 新增因子归因列: {col_def[0]}")
                    except Exception:
                        pass  # 列已存在

                # P0 (2026-07-26 扫尾): 全新 DB 引导缺列 — save_trade/upsert_open_position 引用
                # 但 _init_db 从未创建 (生产 DB 靠历年 ad-hoc ALTER 存活; 与 06-23 因子列同病)。
                # 全新部署/临时 DB 上必抛 OperationalError。清单 = 生产 schema 与全新 DB 实测 diff。
                for stmt in [
                    "ALTER TABLE trade_records ADD COLUMN config_version TEXT",
                    "ALTER TABLE trade_records ADD COLUMN data_quality TEXT",
                    "ALTER TABLE trade_records ADD COLUMN entry_session TEXT",
                    "ALTER TABLE trade_records ADD COLUMN entry_vix REAL",
                    "ALTER TABLE trade_records ADD COLUMN mae_pct REAL",
                    "ALTER TABLE trade_records ADD COLUMN mfe_pct REAL",
                    "ALTER TABLE open_positions ADD COLUMN account_type TEXT",
                    "ALTER TABLE open_positions ADD COLUMN entry_broker_order_id TEXT",
                    "ALTER TABLE open_positions ADD COLUMN entry_context_json TEXT",
                    "ALTER TABLE open_positions ADD COLUMN expected_hold_days INTEGER",
                    "ALTER TABLE open_positions ADD COLUMN invalidation_price REAL",
                    "ALTER TABLE open_positions ADD COLUMN metadata_json TEXT",
                    "ALTER TABLE open_positions ADD COLUMN owner TEXT",
                    "ALTER TABLE open_positions ADD COLUMN sl_broker_order_id TEXT",
                    "ALTER TABLE open_positions ADD COLUMN stop_loss_price REAL",
                    "ALTER TABLE open_positions ADD COLUMN stop_loss_triggered INTEGER",
                    "ALTER TABLE open_positions ADD COLUMN thesis TEXT",
                    "ALTER TABLE open_positions ADD COLUMN tp_broker_order_id TEXT",
                    "ALTER TABLE open_positions ADD COLUMN trader_type TEXT",
                    "ALTER TABLE open_positions ADD COLUMN trailing_stop_price REAL",
                    "ALTER TABLE open_positions ADD COLUMN trailing_stop_triggered INTEGER",
                ]:
                    try:
                        conn.execute(stmt)
                    except Exception:
                        pass  # 列已存在

                # 一次性数据迁移: 把误存入 sl_broker_order_id 的入场单号迁到
                # entry_broker_order_id. 包裹 try/except — 遗留 DB 可能缺少
                # 引用列 (futu_order_id / sl_broker_order_id / entry_broker_order_id),
                # 迁移失败不应阻断初始化 (2026-10-09, test_legacy_db 实证).
                try:
                    cursor = conn.execute("""
                        UPDATE open_positions AS op
                        SET entry_broker_order_id=COALESCE(
                                entry_broker_order_id, op.sl_broker_order_id),
                            sl_broker_order_id=NULL
                        WHERE COALESCE(op.sl_broker_order_id, '') <> ''
                          AND EXISTS (
                              SELECT 1
                              FROM trade_records AS tr
                              WHERE tr.symbol=op.symbol
                                AND tr.futu_order_id=op.sl_broker_order_id
                          )
                    """)
                    if cursor.rowcount:
                        logger.info(
                            "[TradeStore] 迁移入场单号到 entry_broker_order_id: %s 行",
                            cursor.rowcount,
                        )
                except Exception as _mig_e:
                    # W456 (2026-10-09): except 内不用 debug (INFO 下不可见 = 静默);
                    #   遗留库迁移失败必须可见且带栈 — 逢遗留 schema 一次性可见,
                    #   新库永不触发, 不构成噪音。
                    logger.warning(
                        "[TradeStore] 入场单号迁移跳过 (遗留 schema 缺列): %s",
                        _mig_e, exc_info=True,
                    )

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

        # Phase 2 (2026-05-31): LLM 归因调度器需要的扩展字段
        for col_def in [
            ("trade_id",                     "ALTER TABLE trade_attribution ADD COLUMN trade_id TEXT"),
            ("key_decision_factor",          "ALTER TABLE trade_attribution ADD COLUMN key_decision_factor TEXT"),
            ("should_have_done_differently", "ALTER TABLE trade_attribution ADD COLUMN should_have_done_differently TEXT"),
            ("specific_suggestion",          "ALTER TABLE trade_attribution ADD COLUMN specific_suggestion TEXT"),
            ("is_systemic_issue",            "ALTER TABLE trade_attribution ADD COLUMN is_systemic_issue INTEGER DEFAULT 0"),
            ("systemic_pattern",             "ALTER TABLE trade_attribution ADD COLUMN systemic_pattern TEXT"),
            ("root_cause_category",          "ALTER TABLE trade_attribution ADD COLUMN root_cause_category TEXT"),
            ("confidence",                   "ALTER TABLE trade_attribution ADD COLUMN confidence REAL"),
            ("llm_model",                    "ALTER TABLE trade_attribution ADD COLUMN llm_model TEXT"),
            ("human_feedback",               "ALTER TABLE trade_attribution ADD COLUMN human_feedback TEXT"),
        ]:
            try:
                conn.execute(col_def[1])
                logger.debug(f"[TradeStore.attribution] Phase2 新增列: {col_def[0]}")
            except Exception:
                pass

        # 3 个索引: symbol/decision_id/entry_time
        for idx_def in [
            "CREATE INDEX IF NOT EXISTS idx_attr_symbol ON trade_attribution(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_attr_decision ON trade_attribution(decision_id)",
            "CREATE INDEX IF NOT EXISTS idx_attr_trace ON trade_attribution(trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_attr_entry_time ON trade_attribution(entry_time)",
            "CREATE INDEX IF NOT EXISTS idx_attr_trade_date ON trade_attribution(trade_date)",
            "CREATE INDEX IF NOT EXISTS idx_attr_owner ON trade_attribution(owner_strategy)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attr_trade_id ON trade_attribution(trade_id) WHERE trade_id IS NOT NULL",
        ]:
            try:
                conn.execute(idx_def)
            except Exception:
                pass

    def save_trade(self, record: Dict[str, Any], voting_data: Optional[Dict[str, Any]] = None) -> int:
        """
        保存一笔完整交易记录

        Args:
            record: dict，包含交易信息和投票详情
                新增支持:
                  - source_type: 来源分类 (MAIN_PIPELINE/ORB_ENGINE/PLAYBOOK_SHADOW/LLM_PLAN/LLM_RECOMMEND)
                  - entry_reason: 下单理由 (JSON 数组或字符串)
                  - signal_details: 信号详情 (JSON 对象)
                  - metadata_json: 扩展元数据 (JSON 对象)
            voting_data: Phase 2 (2026-05-31) 可选投票数据快捷入口
                如传入, 会覆盖/合并 record 中的 voting_result/model_scores/consensus_level
                结构: {
                    "voting_result": {...},
                    "model_scores": [...],
                    "consensus_level": 0.85,
                    "final_strength": 75,
                }
        
        Returns:
            新记录的 rowid
        """
        record["exit_reason"] = normalize_exit_reason(record.get("exit_reason"))
        # Phase 2 (2026-05-31): 兼容外部直接传入 voting_data
        if voting_data:
            if voting_data.get("voting_result") is not None:
                record.setdefault("voting_result", voting_data["voting_result"])
            if voting_data.get("model_scores") is not None:
                record.setdefault("model_scores", voting_data["model_scores"])
            if voting_data.get("consensus_level") is not None:
                record.setdefault("consensus_level", voting_data["consensus_level"])
            if voting_data.get("final_strength") is not None:
                record.setdefault("final_strength", voting_data["final_strength"])
        import json
        import logging as _logging
        _logger = _logging.getLogger(__name__)
        
        # P0 数据质量守卫 (2026-05-22): 关键归因字段完整性检查
        _symbol = record.get("symbol", "")
        ensure_test_symbol_not_in_production_db(_symbol, self.db_path)
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
            _dq_msg = (
                f"[数据质量守卫] {_symbol} save_trade 缺少归因字段: {_missing_fields} | "
                f"source={record.get('source_type', 'UNKNOWN')} | "
                f"trace_id={record.get('trace_id', 'NONE')}"
            )
            # 2026-08-03 (任务#6 降噪): PLAYBOOK 路径系统性缺 strategy_id/exit_reason
            #   (已知缺口, 10+条/天无行动) → DEBUG; 其他来源保持 WARNING (可行动).
            if (record.get("source_type") or "").upper().startswith("PLAYBOOK"):
                _logger.debug(_dq_msg)
            else:
                _logger.warning(_dq_msg)

        # Task #36 (2026-06-16): signal_strength + signal_id 强制存在 (NOT NULL).
        # 手动单允许 signal_strength=0 + signal_id="manual:<id>"; 策略单必须含真值.
        # 完全缺失 (None) → 拒绝写库 (ERROR + raise), 防止 NULL 沉默蔓延.
        _src_type = (record.get("source_type") or "").upper()
        _is_manual_src = _src_type in ("MANUAL", "MANUAL_CLOSE", "")
        _ss = record.get("signal_strength")
        _sid = record.get("signal_id")
        if _ss is None:
            if _is_manual_src:
                record["signal_strength"] = 0  # 手动单缺省值
            else:
                _logger.error(
                    f"[Task#36·signal_strength守卫] {_symbol} 拒绝写库: "
                    f"signal_strength=None | source_type={_src_type} | "
                    f"trace_id={record.get('trace_id', 'NONE')}"
                )
                raise ValueError(
                    f"trade_records.signal_strength 不可为 None (symbol={_symbol}, "
                    f"source_type={_src_type}); 调用方必须显式提供 signal_strength."
                )
        if not _sid:
            if _is_manual_src:
                _futu_id = record.get("futu_order_id") or record.get("close_futu_order_id") or ""
                record["signal_id"] = f"manual:{_futu_id}" if _futu_id else "manual"
            else:
                # 策略单允许 trace_id 兜底 (line 616 已实现), 但若两者皆空则 raise
                if not record.get("trace_id"):
                    _logger.error(
                        f"[Task#36·signal_id守卫] {_symbol} 拒绝写库: "
                        f"signal_id 与 trace_id 均缺失 | source_type={_src_type}"
                    )
                    raise ValueError(
                        f"trade_records.signal_id 不可为空 (symbol={_symbol}, "
                        f"source_type={_src_type}); 必须提供 signal_id 或 trace_id."
                    )

        # ── P3 (2026-06-16): exit_price 数据质量守卫 ──
        # 根因: NVDA 手动平仓 exit_price=0 → 假亏损 $6155 → 触发日熔断 → 整晚无法开仓
        # 修法: exit_price ≤ 0 的交易记录拒绝写入, 避免假亏损污染熔断计算
        _exit_px = record.get("exit_price", 0)
        _exit_reason = (record.get("exit_reason") or "").upper()
        _is_open_only = _exit_reason in ("", "OPEN", "ENTRY")  # 仅开仓无平仓
        if _exit_px is not None and float(_exit_px) <= 0 and not _is_open_only:
            _logger.error(
                f"[exit_price守卫] {_symbol} 拒绝写入: exit_price={_exit_px} ≤ 0 "
                f"| exit_reason={_exit_reason} | source_type={_src_type} | "
                f"realized_pnl={record.get('realized_pnl', '?')}"
            )
            raise ValueError(
                f"trade_records.exit_price 不可为 0 (symbol={_symbol}); "
                f"必须从 Broker 成交回报或订单限价获取实际成交价."
            )

        # 幽灵记录守卫 (2026-07-03): entry_price=exit_price AND realized_pnl=0 → 拒绝写入
        # 根因: position_monitor 在平仓确认时未取到真实市场价, 用 entry_price 填充 exit_price,
        # 产生 pnl=0 的虚假交易记录, 污染 dashboard/review 统计.
        # 已有 exit_price≤0 守卫在此之上, 此处拦截 exit_price>0 但 entry=exit 的幽灵记录.
        _entry_px_check = record.get("entry_price")
        _pnl_check = record.get("realized_pnl", 0)
        if (
            _exit_px is not None
            and _entry_px_check is not None
            and float(_exit_px) > 0
            and float(_exit_px) == float(_entry_px_check)
            and float(_pnl_check or 0) == 0
            and not _is_open_only
        ):
            _logger.error(
                f"[幽灵记录守卫] {_symbol} 拒绝写入: entry_price=exit_price={_entry_px_check} "
                f"AND realized_pnl=0 | exit_reason={_exit_reason} | source_type={_src_type} | "
                f"close_futu_order_id={record.get('close_futu_order_id', 'NONE')}"
            )
            raise ValueError(
                f"幽灵交易记录拒绝写入 (symbol={_symbol}): entry_price=exit_price={_entry_px_check} "
                f"且 realized_pnl=0; position_monitor 应传入真实 exit_price 而非 entry_price."
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

                # 处理 metadata_json (RFC-061: 注入 A/B cohort)
                metadata_json = record.get("metadata_json")
                if isinstance(metadata_json, str):
                    try:
                        metadata_json = json.loads(metadata_json)
                    except (ValueError, TypeError):
                        metadata_json = {}
                if not isinstance(metadata_json, dict):
                    metadata_json = {}
                # 注入 cohort (确定性 hash, 幂等) — 无 symbol/trade_date 时落 A 组
                metadata_json["cohort"] = assign_cohort(
                    record.get("symbol", "") or "",
                    record.get("trade_date", "") or "",
                )
                metadata_json = json.dumps(metadata_json, ensure_ascii=False, default=str)

                # P0 (2026-06-07): factor_detail 处理
                # 2026-07-09: 修复list类型 — _row_to_dict自动json.loads了conditions_json,
                # 使其从string变成list, 但此处只处理dict, 导致sqlite3 "unsupported type"
                factor_detail = record.get("factor_detail")
                if isinstance(factor_detail, (dict, list)):
                    factor_detail = json.dumps(factor_detail, ensure_ascii=False, default=str)
                elif not factor_detail:
                    factor_detail = None

                # P1 (2026-06-10): 因子归因落库 — 从 factor_detail 提取各因子 raw score
                # IntradayScorer 5因子: trend, momentum, volume, orderflow, lifecycle
                # 2026-07-16: 优先用 record 显式传入的 f1-f5 (Playbook 路径按 conditions 自算好),
                #   仅当 record 未携带任一 f 键时, 才从 factor_detail 5因子 dict 反解 (MAIN_PIPELINE).
                #   根因: 旧逻辑无条件从 factor_detail 反解, 而 Playbook 的 factor_detail 是 conditions
                #   字符串数组 (非 5因子 dict) → 反解成空 → f1-f5 全 0, Playbook 因子归因瘫痪.
                _f_keys = ("f1_trend", "f2_momentum", "f3_volume", "f4_orderflow", "f5_lifecycle")
                if any(k in record for k in _f_keys):
                    _f1_trend = float(record.get("f1_trend", 0) or 0)
                    _f2_momentum = float(record.get("f2_momentum", 0) or 0)
                    _f3_volume = float(record.get("f3_volume", 0) or 0)
                    _f4_orderflow = float(record.get("f4_orderflow", 0) or 0)
                    _f5_lifecycle = float(record.get("f5_lifecycle", 0) or 0)
                else:
                    _fd_raw = record.get("factor_detail")
                    if isinstance(_fd_raw, str):
                        try:
                            _fd_raw = json.loads(_fd_raw)
                        except Exception:
                            _fd_raw = {}
                    if not isinstance(_fd_raw, dict):
                        _fd_raw = {}
                    _f1_trend = float((_fd_raw.get("trend") or {}).get("raw", 0))
                    _f2_momentum = float((_fd_raw.get("momentum") or {}).get("raw", 0))
                    _f3_volume = float((_fd_raw.get("volume") or {}).get("raw", 0))
                    _f4_orderflow = float((_fd_raw.get("orderflow") or {}).get("raw", 0))
                    _f5_lifecycle = float((_fd_raw.get("lifecycle") or {}).get("raw", 0))

                # 未了结行 realized_pnl 写 NULL 不写 0.0: 下游以 "realized_pnl IS NOT NULL" 作已平仓代理
                # (position_sync external_close 查重 / pre_entry_gate 历史), 0.0 占位让在途仓伪装成保本平仓
                # → 真实平仓被误判重复而连通知一起丢弃, 胜率分母也掺 0 笔.
                # 判据取"有任何出场痕迹"而非仅 exit_price: 平仓时行情坏到 0 价也必须保留真实盈亏.
                _has_exit = bool(record.get("exit_price") or record.get("exit_time"))
                _realized_pnl_val = record.get("realized_pnl") if _has_exit else None

                cursor = conn.execute("""
                    INSERT INTO trade_records (
                        trade_date, symbol, direction, entry_time, exit_time,
                        entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                        fees_est, pnl_net, fees_source, net_pnl_pct,
                        exit_reason, holding_hours, signal_strength, signal_confidence,
                        futu_order_id, close_futu_order_id, voting_result, final_strength, consensus_level,
                        model_scores, strategy_id, decision_reason, max_drawdown, trace_id,
                        source_type, entry_reason, signal_details, metadata_json,
                        owner_strategy, mode, market_regime, trade_mode,
                        signal_id, decision_id, factor_detail,
                        f1_trend, f2_momentum, f3_volume, f4_orderflow, f5_lifecycle,
                        created_at, config_version, entry_vix, entry_session
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'v2',?,?)
                """, (
                    record.get("trade_date", ""),
                    record.get("symbol", ""),
                    record.get("direction", ""),
                    record.get("entry_time", ""),
                    record.get("exit_time") or None,  # ponytail: NULL not '' — 消灭 exit_time 空字符串/NULL 不一致
                    record.get("entry_price", 0.0),
                    # ponytail: 开仓时 exit_price=0 → 写 NULL (与 exit_time IS NULL 语义对齐)
                    record.get("exit_price") if record.get("exit_price") else None,
                    record.get("quantity", 0),
                    _realized_pnl_val,
                    record.get("pnl_pct", 0.0),
                    record.get("fees_est"),
                    record.get("pnl_net"),
                    record.get("fees_source"),
                    record.get("net_pnl_pct"),
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
                    record.get("strategy_id", "") or record.get("owner_strategy", "main_pipeline"),
                    record.get("decision_reason", ""),
                    record.get("max_drawdown", 0.0),
                    record.get("trace_id", ""),
                    record.get("source_type", "MAIN_PIPELINE"),
                    entry_reason,
                    signal_details,
                    metadata_json,
                    record.get("owner_strategy") or "main",
                    record.get("mode") or _get_runtime_mode(),
                    record.get("market_regime") or "UNKNOWN",
                    record.get("trade_mode") or _get_trade_mode(),
                    record.get("signal_id") or record.get("trace_id") or "",
                    record.get("decision_id") or "",
                    factor_detail,
                    _f1_trend, _f2_momentum, _f3_volume, _f4_orderflow, _f5_lifecycle,
                    datetime.now().isoformat(),
                    record.get("entry_vix", 0.0) or 0.0,
                    record.get("entry_session", "") or "",
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

    def query_by_date(self, trade_date: str, trade_mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        按交易日期查询记录

        Args:
            trade_date: 'YYYY-MM-DD'
            trade_mode: 可选 'simulate' / 'live' 过滤 (Phase 1 2026-05-31)
        """
        with self._lock:
            conn = self._get_conn()
            try:
                # 2026-09-02 (DEB-20260902-mfe-caliber-and-shadow-zero 议题三): row_factory
                #   按名映射 — 原 _row_to_dict 位置映射在 8/27 补 decision_id 列后未同步,
                #   dict["created_at"] 实装 decision_id 值、decision_id 键缺失 → P2b 轮次级
                #   联接仍读 None, 8/27 "补列即恢复守卫" 实际未生效 (实测 9/1 全 14 行复现)。
                conn.row_factory = sqlite3.Row
                if trade_mode:
                    rows = conn.execute("""
                        SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                               entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                               exit_reason, holding_hours, signal_strength, signal_confidence,
                               futu_order_id, source_type, decision_id, created_at, metadata_json
                        FROM trade_records
                        WHERE trade_date = ? AND trade_mode = ?
                        ORDER BY entry_time ASC
                    """, (trade_date, trade_mode)).fetchall()
                else:
                    # 2026-08-27 (P1 幻影回填根修): SELECT 曾缺 decision_id —
                    #   shadow_tracker P2b "轮次级联接" (2026-08-24) 读 t.get("decision_id")
                    #   恒 None → 全部成交被视为"无归属" → intraday_reentry 新轮行认领
                    #   上一轮成交 (实锤 8/26 #1777 被盖 #1370 的 executed/pnl/outcome)。
                    #   补列即恢复 P2b 守卫; 纯增量字段, 其余消费者不受影响。
                    rows = conn.execute("""
                        SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                               entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                               exit_reason, holding_hours, signal_strength, signal_confidence,
                               futu_order_id, source_type, decision_id, created_at, metadata_json
                        FROM trade_records
                        WHERE trade_date = ?
                        ORDER BY entry_time ASC
                    """, (trade_date,)).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def query_today(self, trade_mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询今日交易记录 (可按 trade_mode 过滤)"""
        today = _et_trade_date(now_et())
        return self.query_by_date(today, trade_mode=trade_mode)

    def query_range(self, start: str, end: str, trade_mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        查询日期区间内的交易记录

        Args:
            start: 'YYYY-MM-DD'
            end:   'YYYY-MM-DD'
            trade_mode: 可选 'simulate' / 'live' 过滤 (Phase 1 2026-05-31)
        """
        with self._lock:
            conn = self._get_conn()
            try:
                # 2026-09-02 (DEB-20260902 议题三): 列集对齐 query_by_date — 原 17 列 SELECT
                #   共用 19 列位置映射, 调用即 IndexError (全仓零调用方, 死方法不自知);
                #   _row_to_dict 已改按名映射, 补齐列集即恢复可用。
                conn.row_factory = sqlite3.Row
                if trade_mode:
                    rows = conn.execute("""
                        SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                               entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                               exit_reason, holding_hours, signal_strength, signal_confidence,
                               futu_order_id, source_type, decision_id, created_at, metadata_json
                        FROM trade_records
                        WHERE trade_date BETWEEN ? AND ? AND trade_mode = ?
                        ORDER BY entry_time ASC
                    """, (start, end, trade_mode)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                               entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                               exit_reason, holding_hours, signal_strength, signal_confidence,
                               futu_order_id, source_type, decision_id, created_at, metadata_json
                        FROM trade_records
                        WHERE trade_date BETWEEN ? AND ?
                        ORDER BY entry_time ASC
                    """, (start, end)).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def stats_by_date(self, trade_date: str, trade_mode: Optional[str] = None) -> Dict[str, Any]:
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
        records = self.query_by_date(trade_date, trade_mode=trade_mode)
        if not records:
            return {"total_trades": 0, "total_pnl": 0.0}

        # 2026-06-26: 过滤 PHANTOM_RECONCILE_VOID (pnl=0 的幽灵对账记录, 不应计入交易统计)
        records = [r for r in records if r.get("exit_reason") != "PHANTOM_RECONCILE_VOID"]
        # 日绩效只统计已了结行: 未平仓行 realized_pnl=NULL (旧数据为 0.0 占位), 既不可参与
        # sum/比较, 也不该按 <=0 被算作亏损。
        records = [r for r in records if r.get("realized_pnl") is not None]
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

    def stats_today(self, trade_mode: Optional[str] = None) -> Dict[str, Any]:
        """统计今日交易绩效 (可按 trade_mode 过滤)"""
        return self.stats_by_date(_et_trade_date(now_et()), trade_mode=trade_mode)

    # ------------------------------------------------------------------
    # 未平仓持仓快照（重启后恢复用）
    # ------------------------------------------------------------------

    def upsert_position_simple(self, symbol: str, direction: str, size: int,
                               entry_price: float, owner_strategy: str = 'playbook_sim',
                               owner: str = 'auto', account_type: str = 'simulate',
                               source_type: str = '', futu_order_id: str = '',
                               stop_loss: float = 0, sync_status: str = 'synced',
                               entry_time: str = '') -> None:
        """简单UPSERT open_positions — 不需要Position对象 (2026-07-02)

        用途: playbook_service下单后立即写open_positions, 使其成为SSOT.
        position_sync发现DB有记录 → 走UPDATE分支 → 不会误标manual.

        2026-07-02 fix: 移除 futu_order_id/source_type 列引用 (open_positions schema 无此列).
        futu_order_id 存入 entry_broker_order_id; sl_broker_order_id 只承载真实 STOP 单号.
        source_type 不存入 open_positions (仅在 trade_records 中保留).

        entry_time (2026-09-06 R-12b 裁决): 传入权威成交时刻 (如 trade_records 的
        entry_time), 两表同源消除结构漂移; 空串 = 旧行为 (自取 now_et()).
        注意 ON CONFLICT 分支本就不刷新 entry_time, 该语义不变.
        """
        from datetime import datetime as _dt
        # ── R-12 (2026-08-23 DEB-20260823): entry_time 统一 ET 口径 ──
        # 原用裸 _dt.now() = 宿主 Asia/Shanghai (CST), 而同一笔仓写进 trade_records 的
        # entry_time 是 ET (playbook_service 侧用 now_et()) → 两表相差恰好 12h。
        # 实证: US.WEAV open_positions=2026-08-19T21:36:54 vs
        #       trade_records=2026-08-19T09:36:54。
        # 同一逻辑事件两种时间约定 = 任何跨表 JOIN、或把 open_positions.entry_time 喂给
        # 按 ET 判断的函数 (如 _is_overnight_position) 都会立刻出错的口径地雷。统一为 ET。
        try:
            from app.utils.timezone import now_et as _now_et_ts
            _now = _now_et_ts().isoformat()
        except Exception:
            _now = _dt.now().isoformat()
        # R-12b (2026-09-06): 优先用调用方传入的成交时刻; updated_at 恒为写入时刻.
        _entry_ts = entry_time or _now
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO open_positions
                (symbol, direction, size, avg_entry_price, initial_sl, current_sl,
                 entry_time, updated_at, strategy_id, trader_type, owner_strategy,
                 owner, sync_status, is_filled, mode, account_type,
                 entry_broker_order_id, sl_broker_order_id,
                 stop_loss_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    direction=excluded.direction, size=excluded.size,
                    avg_entry_price=excluded.avg_entry_price,
                    current_sl=excluded.current_sl, updated_at=excluded.updated_at,
                    owner_strategy=excluded.owner_strategy, owner=excluded.owner,
                    sync_status=excluded.sync_status, account_type=excluded.account_type,
                    entry_broker_order_id=excluded.entry_broker_order_id,
                    stop_loss_price=excluded.stop_loss_price,
                    trader_type=excluded.trader_type,
                    strategy_id=excluded.strategy_id
            """, (symbol, direction, size, entry_price, stop_loss, stop_loss,
                  _entry_ts, _now, owner_strategy, 'AUTO', owner_strategy,
                  owner, sync_status, 'normal', account_type, futu_order_id,
                  None, stop_loss))
            conn.commit()
        finally:
            if hasattr(conn, 'close'):
                conn.close()

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
                # P0 修复 (2026-06-15): 同时读取 owner/thesis/entry_context 等列
                # INSERT OR REPLACE 会删除旧行重建, 不显式写入的列会丢失为默认值
                # SNDK/MU 事件根因: owner='manual' 被重置为默认 'auto'
                row = conn.execute(
                    "SELECT version, is_anomalous, owner, thesis, entry_context_json, "
                    "trader_type, invalidation_price, expected_hold_days, "
                    "stop_loss_price, trailing_stop_price, sl_broker_order_id "
                    "FROM open_positions WHERE symbol = ?",
                    (position.symbol,)
                ).fetchone()
                current_version = (row[0] if row else 0) + 1
                # P0 (2026-06-15): 保留 INSERT OR REPLACE 会丢失的列
                _preserved_owner = row[2] if (row and row[2]) else 'auto'
                _preserved_thesis = row[3] if (row and row[3]) else ''
                _preserved_entry_ctx = row[4] if (row and row[4]) else '{}'
                _preserved_trader_type = row[5] if (row and row[5]) else 'MANUAL'
                _preserved_invalidation = row[6] if (row and row[6] is not None) else 0
                _preserved_hold_days = row[7] if (row and row[7] is not None) else 5
                _preserved_sl_price = row[8] if row else None  # can be None
                _preserved_trail_price = row[9] if row else None  # can be None
                _preserved_sl_broker_id = row[10] if (row and row[10]) else ''
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

                # P0 (2026-06-07): 序列化 position.metadata → open_positions.metadata_json
                # 根因: position.metadata 仅存于内存, 重启/position_sync 后丢失
                # 后果: 平仓时 factor_detail/trace_id/entry_reason 全部为空
                _meta_raw = getattr(position, "metadata", None) or {}
                try:
                    _meta_json = json.dumps(_meta_raw, ensure_ascii=False, default=str) if _meta_raw else None
                except Exception:
                    _meta_json = None

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
                        metadata_json,
                        owner, thesis, entry_context_json, trader_type,
                        invalidation_price, expected_hold_days,
                        stop_loss_price, trailing_stop_price, sl_broker_order_id,
                        updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    # last_sync_at → ET (2026-09-05 M-6/B1 第三阶): 与读方 _parse_sync_ts 同尺闭环
                    now_et().isoformat(),
                    int(is_filled),
                    int(is_partially_reduced),
                    int(preserve_anomalous or (getattr(position, "metadata", {}) or {}).get("_close_blocked", False)),
                    trace_id,
                    action_log_json,
                    # Phase A: 写 owner_strategy
                    getattr(position, "owner_strategy", None) or "main",
                    _get_runtime_mode(),
                    _meta_json,
                    # P0 (2026-06-15): 保留 INSERT OR REPLACE 会丢失的列
                    _preserved_owner,
                    _preserved_thesis,
                    _preserved_entry_ctx,
                    _preserved_trader_type,
                    _preserved_invalidation,
                    _preserved_hold_days,
                    _preserved_sl_price,
                    _preserved_trail_price,
                    _preserved_sl_broker_id,
                    # ⚠️ 尺度债 — B1 第五阶范围(原误标"第四阶", DEB §12.6 更正), 本批**刻意不动**: updated_at 仍写 naive,
                    #   与同一条 INSERT 里的 last_sync_at(ET) / entry_time(ET) **不同尺**。不顺手翻的
                    #   实证理由: 该列有真消费方做字符串排序与比较 (position_monitor:1229
                    #   `ORDER BY updated_at DESC LIMIT 1`、position_monitor:1164 `updated_at>=?`),
                    #   且它**已经在混存** (本处 naive vs upsert_position_simple 的 now_et) ⇒ 不是
                    #   "翻后才混存", 必须先出消费方清单 + 归一/打标方案, 单独 DEB 裁决。
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
        """平仓后物理删除持仓记录

        2026-07-14: 软删除→直接DELETE (trade_records已有完整审计记录).
        来源: scheduler/sync/emergency, 均直接DELETE.
        """
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT symbol FROM open_positions WHERE symbol = ?",
                    (symbol,)
                ).fetchone()
                if not row:
                    return
                conn.execute(
                    "DELETE FROM open_positions WHERE symbol=?",
                    (symbol,)
                )
                conn.commit()
                logger.info(
                    f"[TruthSource] 仓位物理删除 {symbol} "
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
                           action_log_json, last_sync_at, owner_strategy
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
                        "owner_strategy":    r[24] if len(r) > 24 else "",
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
                """, (now_et().isoformat(), trace_id, symbol))  # ET: B1 第三阶
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
                """, (now_et().isoformat(), symbol))  # ET: B1 第三阶
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
                """, (now_et().isoformat(), symbol))  # ET: B1 第三阶
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
                """, (new_version, now_et().isoformat(), symbol))  # ET: B1 第三阶
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
                """, (value, new_version, source, now_et().isoformat(), trace_id,  # ET: B1 第三阶
                      action_entry, action_entry, symbol))
                conn.commit()
                return True
            finally:
                conn.close()

    def get_pending_sync_positions(self, max_age_seconds: int = 300) -> List[str]:
        """获取超过指定时间仍未同步的仓位 (用于告警).

        ⚠️ **零消费方** (2026-09-05 全仓实测, DEB-20260905 M-6/B2 复算): 本函数在仓内只有
        这一处 def, 调用方为零; REPORT D-06 声称的调用链
        `scheduler_core._position_sync_loop → get_pending_sync_positions(300)` 中
        `_position_sync_loop` **在仓内不存在** ⇒ 这条"同步卡住"告警**从未发出过**, 而非
        D-06 判的"延迟 8h" (影响面已按实情更正入 DEB §十)。`sync_status` 字段本身是活的
        (写: web_dashboard/order/audit.py 手动单落 'pending_sync'; 读: app/core/position_sync.py
        判 needs_data_fix), 但**超时告警这一层是纸面的**。接线 = 新增告警环路 (须入
        docs/CONTROL-LOOP-LEDGER.md), 本批不代裁; 先把判据修对, 免接线当天即静默漏报 8h。

        时区修复 (D-06 的**机制**成立): 原判据把 cutoff 交给 SQLite —
        `datetime(last_sync_at) < datetime('now','-300 seconds')`; 而 last_sync_at 当时由
        **6 个**写方以 naive 本地(CST)墙钟写入 (前批记的"3 处"是漏盘: 除 mark_sync_pending /
        mark_sync_complete / mark_anomalous 外, 还有 upsert_open_position / unmark_anomalous /
        update_position_field; 其中 upsert_open_position 是最热路径 ⇒ 只翻 3 处会让该列**永久**
        混存而非过渡期混存), SQLite `datetime('now')` = **UTC**
        → 库内时刻被当成"8 小时之后", 判据在 8h 内恒 False (**漏报方向**: 卡住的同步不告警,
        直到 CST 墙钟老化到比 UTC now 更早才触发 = 感知窗口 5min 膨胀到 8h+)。
        现改 Python 侧解析比较, 同尺原则:
          - 带 tz 的值按其偏移换算 (**已是现役格式**: 写方 6 处已在 B1 第三阶统一为 now_et(),
            本函数一行未改即兼容 — 这正是当初把读方做成格式兼容的目的);
          - naive 值一律按 **CST** 解释 = 与**旧**写入方同一把尺子 (兜底分支, 见 _parse_sync_ts);
          - NULL / 脏格式解析不出 → **保守计入告警** (fail-loud: 宁可多告一次, 不可让卡住的
            同步静默 — 与"读取失败=保守方向"同哲学)。
        """
        _cutoff = datetime.now(CST) - timedelta(seconds=max(0, int(max_age_seconds)))
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT symbol, last_sync_at FROM open_positions
                    WHERE sync_status = 'pending_sync'
                """).fetchall()
            finally:
                conn.close()
        _stale: List[str] = []
        for _sym, _raw in rows:
            _ts = _parse_sync_ts(_raw)
            if _ts is None:
                _why = ("从未写过 last_sync_at" if not _raw
                        else f"last_sync_at 不可解析 ({_raw!r})")
                logger.warning(
                    f"[TruthSource] {_sym} 处于 pending_sync 但 {_why} → "
                    f"保守计入超时告警 (fail-loud, 宁多告不静默)")
                _stale.append(_sym)
            elif _ts < _cutoff:
                _stale.append(_sym)
        return _stale

    # ------------------------------------------------------------------
    # (Phase 2 市场上下文快照区已退役 — 见下方退役注记)
    # ------------------------------------------------------------------

    # ── save_context_snapshot / get_context_snapshot 退役 (2026-09-13, 用户裁"删除") ──
    #    entry_context_snapshot 死表退役配套: 两函数全仓零调用方 (唯一读者
    #    AttributionScheduler 的调用块同批删除)。入场时刻指标快照活通道 =
    #    trade_records.factor_detail.ts_ctx (F2, DEB-20260804)。
    #    回滚: git revert 本提交。

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        # 2026-09-02 (DEB-20260902-mfe-caliber-and-shadow-zero 议题三): 改按列名映射 —
        #   原位置映射在 SELECT 列序漂移时静默错位 (8/27 补 decision_id 后 created_at 槽
        #   实装 decision_id 值, query_range 更是调用即 IndexError)。要求调用方设置
        #   conn.row_factory = sqlite3.Row (query_by_date/query_range 已设)。
        d = dict(row)
        md = d.get("metadata_json")
        if isinstance(md, str) and md:
            try:
                d["metadata_json"] = json.loads(md)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[trade_store] trade id={d.get('id')} metadata_json "
                               f"解析失败, 保留原始字符串")
        return d
