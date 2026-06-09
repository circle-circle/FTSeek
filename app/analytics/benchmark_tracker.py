"""
BenchmarkTracker — SPY/QQQ 基准对照与 Alpha 计算

Phase 1 (2026-05-31): FTSeek-US 风险止血 + 基准建立

职责
----
- 每日收盘后记录 SPY / QQQ 收益率到 benchmark_snapshots 表
- 计算策略 vs 基准的 Alpha (超额收益)
- 提供日报使用的基准摘要

数据来源
--------
行情数据从注入的 MarketDataRouter 获取 (走 Futu LV3 → Finnhub → TwelveData 路由).

数据存储
--------
SQLite ftseek_us.db, 表 benchmark_snapshots:
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    spy_close REAL,
    qqq_close REAL,
    spy_return_pct REAL,
    qqq_return_pct REAL,
    strategy_return_pct REAL,
    alpha_vs_spy REAL,
    alpha_vs_qqq REAL,
    created_at TEXT DEFAULT (datetime('now'))

向后兼容
--------
- MarketDataRouter 注入失败时, record_daily_benchmark() 跳过且记录 WARN, 不抛异常
- 缺少历史 close 时, return_pct=None, 不影响 alpha 计算路径下游
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek_us.benchmark_tracker")

try:
    from app.core.db_utils import get_main_db
    DEFAULT_DB_PATH = str(get_main_db())
except ImportError:
    DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db")

# 默认基准代码 (走 MarketDataRouter 的 US.<symbol> 命名空间)
SPY_CODE = "US.SPY"
QQQ_CODE = "US.QQQ"


class BenchmarkTracker:
    """追踪 SPY/QQQ 基准收益, 计算策略 Alpha"""

    def __init__(self, db_path: Optional[str] = None, market_data_router: Any = None):
        """
        Args:
            db_path: SQLite 路径, 默认 ftseek_us.db
            market_data_router: MarketDataRouter 实例 (可选). 注入后用于取实时/历史报价.
        """
        self.db_path = db_path or DEFAULT_DB_PATH
        self.market_data_router = market_data_router
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # 表初始化
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        try:
            with self._lock:
                conn = self._get_conn()
                try:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS benchmark_snapshots (
                            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                            date                TEXT NOT NULL UNIQUE,
                            spy_close           REAL,
                            qqq_close           REAL,
                            spy_return_pct      REAL,
                            qqq_return_pct      REAL,
                            strategy_return_pct REAL,
                            alpha_vs_spy        REAL,
                            alpha_vs_qqq        REAL,
                            created_at          TEXT DEFAULT (datetime('now'))
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_bench_date ON benchmark_snapshots(date)"
                    )
                    conn.commit()
                    logger.debug(
                        "[BenchmarkTracker] 数据库初始化: %s", self.db_path
                    )
                finally:
                    conn.close()
        except Exception as e:  # 不阻塞主流程
            logger.warning("[BenchmarkTracker] _init_db 失败 (跳过): %s", e)

    # ------------------------------------------------------------------
    # 行情取数
    # ------------------------------------------------------------------

    def _resolve_close(self, symbol: str) -> Optional[float]:
        """从 MarketDataRouter 解析当前 close (报价的 last/close 字段)"""
        if self.market_data_router is None:
            return None
        try:
            quote = self.market_data_router.get_quote(symbol)
            if not isinstance(quote, dict):
                return None
            for key in ("close", "price", "last", "last_price"):
                v = quote.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
            return None
        except Exception as e:
            logger.warning("[BenchmarkTracker] get_quote(%s) 失败: %s", symbol, e)
            return None

    def _previous_close(self, symbol: str, before_date: str) -> Optional[float]:
        """从 benchmark_snapshots 表回看上一日 close"""
        try:
            conn = self._get_conn()
            try:
                column = "spy_close" if symbol == SPY_CODE else "qqq_close"
                row = conn.execute(
                    f"SELECT {column} FROM benchmark_snapshots "
                    f"WHERE date < ? AND {column} IS NOT NULL "
                    f"ORDER BY date DESC LIMIT 1",
                    (before_date,),
                ).fetchone()
                if row and isinstance(row[0], (int, float)) and row[0] > 0:
                    return float(row[0])
                return None
            finally:
                conn.close()
        except Exception as e:
            logger.warning(
                "[BenchmarkTracker] _previous_close(%s) 失败: %s", symbol, e
            )
            return None

    # ------------------------------------------------------------------
    # 日度记录
    # ------------------------------------------------------------------

    def record_daily_benchmark(
        self,
        target_date: Optional[str] = None,
        strategy_return_pct: Optional[float] = None,
    ) -> Optional[int]:
        """收盘后记录当日 SPY / QQQ 收益率, 同时计算 Alpha (若提供策略收益).

        Args:
            target_date: ISO 日期 (YYYY-MM-DD), 默认 today
            strategy_return_pct: 当日策略收益率 (%), 缺省时仅记录基准

        Returns:
            新记录 rowid, 或 None (静默失败)
        """
        target_date = target_date or date.today().strftime("%Y-%m-%d")

        spy_close = self._resolve_close(SPY_CODE)
        qqq_close = self._resolve_close(QQQ_CODE)

        if spy_close is None and qqq_close is None:
            logger.warning(
                "[BenchmarkTracker] %s SPY/QQQ close 均无法获取, 跳过记录",
                target_date,
            )
            return None

        spy_prev = self._previous_close(SPY_CODE, target_date)
        qqq_prev = self._previous_close(QQQ_CODE, target_date)

        spy_return = (
            (spy_close - spy_prev) / spy_prev * 100.0
            if (spy_close and spy_prev) else None
        )
        qqq_return = (
            (qqq_close - qqq_prev) / qqq_prev * 100.0
            if (qqq_close and qqq_prev) else None
        )

        alpha_vs_spy = (
            strategy_return_pct - spy_return
            if (strategy_return_pct is not None and spy_return is not None)
            else None
        )
        alpha_vs_qqq = (
            strategy_return_pct - qqq_return
            if (strategy_return_pct is not None and qqq_return is not None)
            else None
        )

        try:
            with self._lock:
                conn = self._get_conn()
                try:
                    cursor = conn.execute(
                        """
                        INSERT OR REPLACE INTO benchmark_snapshots (
                            date, spy_close, qqq_close,
                            spy_return_pct, qqq_return_pct,
                            strategy_return_pct, alpha_vs_spy, alpha_vs_qqq,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_date,
                            spy_close, qqq_close,
                            spy_return, qqq_return,
                            strategy_return_pct, alpha_vs_spy, alpha_vs_qqq,
                            datetime.now().isoformat(),
                        ),
                    )
                    conn.commit()
                    logger.info(
                        "[BenchmarkTracker] %s 已记录 SPY=%s QQQ=%s "
                        "spy_ret=%s qqq_ret=%s alpha_spy=%s alpha_qqq=%s",
                        target_date,
                        f"${spy_close:.2f}" if spy_close else "N/A",
                        f"${qqq_close:.2f}" if qqq_close else "N/A",
                        f"{spy_return:.2f}%" if spy_return is not None else "N/A",
                        f"{qqq_return:.2f}%" if qqq_return is not None else "N/A",
                        f"{alpha_vs_spy:.2f}%" if alpha_vs_spy is not None else "N/A",
                        f"{alpha_vs_qqq:.2f}%" if alpha_vs_qqq is not None else "N/A",
                    )
                    return cursor.lastrowid
                finally:
                    conn.close()
        except Exception as e:
            logger.error("[BenchmarkTracker] record_daily_benchmark 写库失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # Alpha 计算
    # ------------------------------------------------------------------

    def calculate_alpha(
        self,
        strategy_return_pct: float,
        period_days: int = 30,
        benchmark: str = "spy",
    ) -> Dict[str, Optional[float]]:
        """计算策略相对基准的 Alpha (累积收益差).

        Args:
            strategy_return_pct: 策略 period 内累计收益率 (%)
            period_days: 回看窗口天数
            benchmark: 'spy' / 'qqq' / 'both'

        Returns:
            {
              "strategy_return_pct": float,
              "spy_cum_return_pct": float | None,
              "qqq_cum_return_pct": float | None,
              "alpha_vs_spy": float | None,
              "alpha_vs_qqq": float | None,
              "period_days": int,
              "samples": int,
            }
        """
        cutoff = (date.today() - timedelta(days=period_days)).strftime("%Y-%m-%d")
        try:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT spy_return_pct, qqq_return_pct FROM benchmark_snapshots "
                    "WHERE date >= ? ORDER BY date ASC",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[BenchmarkTracker] calculate_alpha 读库失败: %s", e)
            rows = []

        spy_returns = [r[0] for r in rows if isinstance(r[0], (int, float))]
        qqq_returns = [r[1] for r in rows if isinstance(r[1], (int, float))]

        spy_cum = sum(spy_returns) if spy_returns else None
        qqq_cum = sum(qqq_returns) if qqq_returns else None

        result = {
            "strategy_return_pct": strategy_return_pct,
            "spy_cum_return_pct": spy_cum,
            "qqq_cum_return_pct": qqq_cum,
            "alpha_vs_spy": (
                strategy_return_pct - spy_cum if spy_cum is not None else None
            ),
            "alpha_vs_qqq": (
                strategy_return_pct - qqq_cum if qqq_cum is not None else None
            ),
            "period_days": period_days,
            "samples": len(rows),
        }
        return result

    # ------------------------------------------------------------------
    # 日报摘要
    # ------------------------------------------------------------------

    def get_benchmark_summary(self, days: int = 30) -> Dict[str, Any]:
        """日报使用的基准收益汇总.

        Returns:
            {
              "period_days": 30,
              "samples": 18,
              "spy": {"cum_return_pct": ..., "avg_daily_pct": ..., "max_dd": ...},
              "qqq": {...},
              "alpha": {"avg_alpha_vs_spy": ..., "avg_alpha_vs_qqq": ...},
              "latest": {"date": ..., "spy_close": ..., "qqq_close": ...},
            }
        """
        cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        empty = {
            "period_days": days,
            "samples": 0,
            "spy": {},
            "qqq": {},
            "alpha": {},
            "latest": {},
        }
        try:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT date, spy_close, qqq_close, spy_return_pct, qqq_return_pct,"
                    " strategy_return_pct, alpha_vs_spy, alpha_vs_qqq "
                    "FROM benchmark_snapshots WHERE date >= ? ORDER BY date ASC",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[BenchmarkTracker] get_benchmark_summary 读库失败: %s", e)
            return empty

        if not rows:
            return empty

        spy_returns = [r[3] for r in rows if isinstance(r[3], (int, float))]
        qqq_returns = [r[4] for r in rows if isinstance(r[4], (int, float))]
        alpha_spy = [r[6] for r in rows if isinstance(r[6], (int, float))]
        alpha_qqq = [r[7] for r in rows if isinstance(r[7], (int, float))]

        def _safe_avg(xs: List[float]) -> Optional[float]:
            return sum(xs) / len(xs) if xs else None

        latest = rows[-1]
        return {
            "period_days": days,
            "samples": len(rows),
            "spy": {
                "cum_return_pct": sum(spy_returns) if spy_returns else None,
                "avg_daily_pct": _safe_avg(spy_returns),
                "samples": len(spy_returns),
            },
            "qqq": {
                "cum_return_pct": sum(qqq_returns) if qqq_returns else None,
                "avg_daily_pct": _safe_avg(qqq_returns),
                "samples": len(qqq_returns),
            },
            "alpha": {
                "avg_alpha_vs_spy": _safe_avg(alpha_spy),
                "avg_alpha_vs_qqq": _safe_avg(alpha_qqq),
                "samples_spy": len(alpha_spy),
                "samples_qqq": len(alpha_qqq),
            },
            "latest": {
                "date": latest[0],
                "spy_close": latest[1],
                "qqq_close": latest[2],
                "spy_return_pct": latest[3],
                "qqq_return_pct": latest[4],
                "strategy_return_pct": latest[5],
                "alpha_vs_spy": latest[6],
                "alpha_vs_qqq": latest[7],
            },
        }


# ──────────────────────────────────────────────────────────────────────
# 全局单例
# ──────────────────────────────────────────────────────────────────────
_tracker_instance: Optional[BenchmarkTracker] = None
_tracker_lock = threading.Lock()


def get_benchmark_tracker(
    db_path: Optional[str] = None,
    market_data_router: Any = None,
    refresh_router: bool = False,
) -> BenchmarkTracker:
    """获取 BenchmarkTracker 单例.

    Args:
        db_path: 首次创建时使用的数据库路径
        market_data_router: 注入 MarketDataRouter
        refresh_router: True 时强制把已存在的单例 router 替换成新值
    """
    global _tracker_instance
    with _tracker_lock:
        if _tracker_instance is None:
            _tracker_instance = BenchmarkTracker(
                db_path=db_path, market_data_router=market_data_router
            )
        elif refresh_router and market_data_router is not None:
            _tracker_instance.market_data_router = market_data_router
        return _tracker_instance
