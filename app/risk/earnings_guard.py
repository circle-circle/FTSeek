"""
财报期保护 (Phase 3, 2026-05-31)
=================================

提供:
1. 财报日历查询 (SQLite earnings_calendar 表)
2. ATR 上限计算 (财报临近时收紧 ATR, 避免跳空导致 ATR 飙升 5-10x)

设计要点:
- 与现有 app/risk/earnings_risk_guard.py 互补:
  * earnings_risk_guard 关注仓位限制/Flash 阈值/跳空保护等业务规则
  * earnings_guard (本模块) 仅做 ATR 数学约束 (供 atr_stop_engine 调用)
- 与 app/data/earnings_calendar.py 解耦: 这里只读自己的本地表, 入库写入由
  外部数据同步任务负责; 读路径完全本地, 不会阻塞退出决策。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.core.db_utils import get_main_db

logger = logging.getLogger("ftseek.earnings_guard")

DEFAULT_DB_PATH = get_main_db()


class EarningsGuard:
    """财报期保护器 - ATR 约束与窗口判定"""

    def __init__(self, db_path: Optional[Union[str, Path]] = None, config: Optional[Dict[str, Any]] = None):
        self.db_path = str(db_path) if db_path else str(DEFAULT_DB_PATH)
        cfg = config or {}
        self.days_before: int = int(cfg.get("days_before", 2))
        self.days_after: int = int(cfg.get("days_after", 1))
        self.atr_cap_multiplier: float = float(cfg.get("atr_cap_multiplier", 2.5))
        self._ensure_table()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_table(self):
        """确保 earnings_calendar 表存在 (best-effort, 失败仅日志告警)"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS earnings_calendar (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        earnings_date TEXT NOT NULL,
                        source TEXT DEFAULT 'manual',
                        created_at TEXT DEFAULT (datetime('now')),
                        UNIQUE(symbol, earnings_date)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_earnings_symbol ON earnings_calendar(symbol)"
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[EarningsGuard] _ensure_table 失败 (不阻断): %s", e)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def is_near_earnings(self, symbol: str, check_date: Optional[date] = None) -> bool:
        """检查标的是否处于 [check_date - days_before, check_date + days_after] 窗口"""
        if not symbol:
            return False
        if check_date is None:
            # 2026-07-27 修复 (时区批同款漏网): date.today()=CST 比 ET 早 12h 进入/离开
            #   财报窗口, 边界日误拦/误放。统一 ET 交易日口径。
            try:
                from app.utils.timezone import now_et
                check_date = now_et().date()
            except Exception:
                check_date = date.today()
        window_start = check_date - timedelta(days=self.days_before)
        window_end = check_date + timedelta(days=self.days_after)

        # 兼容 'AAPL' 与 'US.AAPL' 两种命名
        candidates = [symbol]
        if symbol.startswith("US."):
            candidates.append(symbol[3:])
        else:
            candidates.append(f"US.{symbol}")

        try:
            conn = sqlite3.connect(self.db_path, timeout=3)
            try:
                placeholders = ",".join("?" * len(candidates))
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM earnings_calendar "
                    f"WHERE symbol IN ({placeholders}) AND earnings_date BETWEEN ? AND ?",
                    (*candidates, window_start.isoformat(), window_end.isoformat()),
                )
                count = cursor.fetchone()[0]
                return count > 0
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[EarningsGuard] is_near_earnings(%s) 查询异常: %s", symbol, e)
            return False

    def get_atr_cap(
        self,
        symbol: str,
        median_atr_14d: float,
        check_date: Optional[date] = None,
    ) -> Optional[float]:
        """临近财报时返回 ATR 上限值, 否则返回 None (不限制)"""
        if median_atr_14d is None or median_atr_14d <= 0:
            return None
        if self.is_near_earnings(symbol, check_date):
            cap = float(median_atr_14d) * self.atr_cap_multiplier
            logger.info(
                "[EarningsGuard] %s near earnings, ATR cap = %.4f (median14d=%.4f x %.2f)",
                symbol, cap, median_atr_14d, self.atr_cap_multiplier,
            )
            return cap
        return None

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def add_earnings_date(self, symbol: str, earnings_date: str, source: str = "manual"):
        """新增/覆盖财报日期 (earnings_date 为 ISO 8601 'YYYY-MM-DD')"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO earnings_calendar (symbol, earnings_date, source) "
                    "VALUES (?, ?, ?)",
                    (symbol, earnings_date, source),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[EarningsGuard] add_earnings_date(%s, %s) 失败: %s", symbol, earnings_date, e)

    def get_upcoming_earnings(self, days_ahead: int = 7) -> List[Dict[str, str]]:
        """获取未来 N 天内有财报的标的"""
        try:  # 2026-07-27: 同上, ET 口径
            from app.utils.timezone import now_et
            today = now_et().date()
        except Exception:
            today = date.today()
        end_dt = today + timedelta(days=days_ahead)
        try:
            conn = sqlite3.connect(self.db_path, timeout=3)
            try:
                cursor = conn.execute(
                    "SELECT symbol, earnings_date FROM earnings_calendar "
                    "WHERE earnings_date BETWEEN ? AND ? ORDER BY earnings_date",
                    (today.isoformat(), end_dt.isoformat()),
                )
                return [{"symbol": r[0], "earnings_date": r[1]} for r in cursor.fetchall()]
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[EarningsGuard] get_upcoming_earnings 异常: %s", e)
            return []


# ============================================================
#  单例
# ============================================================

_singleton: Optional[EarningsGuard] = None


def get_earnings_guard(
    db_path: Optional[Union[str, Path]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> EarningsGuard:
    """获取 EarningsGuard 单例"""
    global _singleton
    if _singleton is None:
        _singleton = EarningsGuard(db_path=db_path, config=config)
    return _singleton


def reset_earnings_guard():
    """测试用: 重置单例"""
    global _singleton
    _singleton = None
