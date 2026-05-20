"""
PnL 异常值监控 (Y5, 2026-05-15)

基于 ORCL +$17,882 / GOOGL -$6,015 异常值教训:
- 单笔 PnL > 5σ 历史均值 → 告警 (可能是 bug / 异常市场事件)
- 不阻断, 但发关键告警 + 写 outliers 表
- 防止单笔异常掩盖系统真实 edge

可控:
- 30 天滚动 mean + std
- 启动加载, 内存 cache
- 失败降级 (无数据时不告警)
"""
from __future__ import annotations
import logging
import sqlite3
import statistics
import time
from typing import Optional

logger = logging.getLogger(__name__)


class PnLOutlierMonitor:
    """PnL 异常值监控 (单例)"""

    def __init__(self, sigma_threshold: float = 5.0, lookback_days: int = 30):
        self._sigma_threshold = sigma_threshold
        self._lookback_days = lookback_days
        self._mean: Optional[float] = None
        self._std: Optional[float] = None
        self._n: int = 0
        self._last_refresh: float = 0.0
        self._refresh_interval_sec = 3600  # 每小时重算一次

    def _ensure_stats(self):
        """从 trade_records 计算滚动 mean/std"""
        if time.time() - self._last_refresh < self._refresh_interval_sec and self._mean is not None:
            return
        try:
            from app.core.paths import get_trade_records_db
            conn = sqlite3.connect(str(get_trade_records_db()))
            cutoff = (time.time() - self._lookback_days * 86400)
            rows = conn.execute(f"""
                SELECT realized_pnl FROM trade_records
                WHERE exit_time IS NOT NULL
                  AND realized_pnl IS NOT NULL
                  AND ABS(realized_pnl) > 0.01
                  AND strftime('%s', exit_time) > ?
            """, (str(cutoff),)).fetchall()
            conn.close()
            pnls = [float(r[0]) for r in rows if r[0] is not None]
            if len(pnls) < 5:
                return
            self._mean = statistics.mean(pnls)
            self._std = statistics.pstdev(pnls) if len(pnls) > 1 else 100
            self._n = len(pnls)
            self._last_refresh = time.time()
            logger.info(
                f"[PnLOutlierMonitor] {self._lookback_days}d 统计刷新: "
                f"n={self._n} mean=${self._mean:.2f} std=${self._std:.2f}"
            )
        except Exception as e:
            logger.debug(f"[PnLOutlierMonitor] 统计计算失败: {e}")

    def check(self, symbol: str, realized_pnl: float, context: Optional[dict] = None) -> bool:
        """
        检查单笔 PnL 是否异常.

        Returns: True 异常 (已告警), False 正常
        """
        self._ensure_stats()
        if self._mean is None or self._std is None or self._std <= 0:
            return False
        z_score = (realized_pnl - self._mean) / self._std
        if abs(z_score) < self._sigma_threshold:
            return False

        # 异常 — 告警
        msg = (
            f"📊 PnL 异常值检测: {symbol} {'+' if realized_pnl > 0 else ''}${realized_pnl:.2f}\n"
            f"30天均值: ${self._mean:.2f}, 标准差: ${self._std:.2f}\n"
            f"Z-score: {z_score:+.1f} ({'盈利' if realized_pnl > 0 else '亏损'} 异常)\n"
            f"含义: {'异常事件抓到 alpha (验证 thesis)' if realized_pnl > 0 else '可能是 bug / 跳空 / 系统失效, 请审查'}"
        )
        logger.warning(f"[PnLOutlierMonitor] {msg}")

        try:
            from app.utils.critical_alerts import send_critical
            send_critical(msg, async_mode=True)
        except Exception:
            pass

        # 写入 DB 留痕
        self._persist(symbol, realized_pnl, z_score, context)
        return True

    def _persist(self, symbol: str, pnl: float, z_score: float, context: Optional[dict]):
        try:
            import json
            from app.core.paths import get_trade_records_db
            conn = sqlite3.connect(str(get_trade_records_db()))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pnl_outliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    realized_pnl REAL,
                    z_score REAL,
                    rolling_mean REAL,
                    rolling_std REAL,
                    rolling_n INTEGER,
                    context_json TEXT,
                    detected_at REAL NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outlier_symbol ON pnl_outliers(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outlier_z ON pnl_outliers(z_score)")
            conn.execute(
                "INSERT INTO pnl_outliers "
                "(symbol, realized_pnl, z_score, rolling_mean, rolling_std, rolling_n, context_json, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol, pnl, z_score, self._mean, self._std, self._n,
                    json.dumps(context or {}, ensure_ascii=False, default=str)[:500],
                    time.time(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[PnLOutlierMonitor·persist] 失败: {e}")


_singleton: Optional[PnLOutlierMonitor] = None


def get_pnl_outlier_monitor() -> PnLOutlierMonitor:
    global _singleton
    if _singleton is None:
        _singleton = PnLOutlierMonitor()
    return _singleton
