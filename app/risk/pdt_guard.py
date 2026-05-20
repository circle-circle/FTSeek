"""
FTSeek-US PDT (Pattern Day Trader) 保护守卫

核心理念: 严格遵守 FINRA PDT 规则，防止账户被券商锁定。
PDT 规则: 资金 < $25,000 的保证金账户，在任意 5 个交易日内最多只能进行 3 次日内交易。

作者: FTSeek Team
日期: 2026-04-21
"""

import logging
import sqlite3
import json
import time
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek.pdt_guard")

class PDTRiskLevel(Enum):
    """PDT 风险等级"""
    SAFE = "SAFE"              # 安全，可以交易
    WARNING = "WARNING"        # 警告，今日最后一次日内交易机会
    CRITICAL = "CRITICAL"      # 危险，禁止日内交易 (仅限隔夜仓)
    LOCKED = "LOCKED"          # 锁定，已达上限或资金不足

@dataclass
class PDTState:
    """PDT 状态快照"""
    account_equity: float = 0.0
    day_trade_count: int = 0  # 过去 5 个交易日内的日内交易次数
    last_reset_date: Optional[str] = None
    risk_level: PDTRiskLevel = PDTRiskLevel.SAFE
    is_pdt_account: bool = False  # 资金是否 >= $25,000

class PDTGuard:
    """
    PDT 保护守卫
    
    职责:
    1. 追踪日内交易次数 (滑动窗口: 5 个交易日)
    2. 检查账户资金是否满足 PDT 豁免条件 ($25,000)
    3. 提供交易许可判断
    4. 持久化状态到 DB
    """
    
    PDT_EQUITY_THRESHOLD = 25000.0
    MAX_DAY_TRADES = 3
    ROLLING_WINDOW_DAYS = 5  # FINRA: 5 个交易日
    
    def __init__(self, db_path: str = None):
        self._db_path = db_path or str(Path(__file__).parent.parent.parent / "data" / "ftseek_us.db")
        self._ensure_db_table()
        self._state = self._load_state()
        
    def _ensure_db_table(self):
        """创建 PDT 状态表"""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pdt_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        timestamp REAL NOT NULL
                    )
                """)
                # 迁移旧数据: 如果 pdt_state 表存在 trade_dates_json 字段
                try:
                    cursor.execute("SELECT trade_dates_json FROM pdt_state WHERE id = 1")
                    row = cursor.fetchone()
                    if row and row[0] and row[0] != '[]':
                        dates = json.loads(row[0])
                        for d in dates:
                            cursor.execute(
                                "INSERT INTO pdt_trades (trade_date, symbol, timestamp) VALUES (?, ?, ?)",
                                (d, "MIGRATED", time.time())
                            )
                        logger.info(f"[PDT] 已迁移 {len(dates)} 条旧交易记录")
                except Exception:
                    pass  # 旧表可能没有此字段

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pdt_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        account_equity REAL DEFAULT 0,
                        last_update TEXT,
                        is_pdt_account INTEGER DEFAULT 0
                    )
                """)
                cursor.execute("SELECT id FROM pdt_state WHERE id = 1")
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO pdt_state (id, last_update) VALUES (1, ?)
                    """, (datetime.now().isoformat(),))
                conn.commit()
        except Exception as e:
            logger.error(f"[PDT] 数据库表初始化失败: {e}")

    @staticmethod
    def _is_trading_day(d: date) -> bool:
        """判断是否为美股交易日 (周一至周五, 简化处理不含节假日)"""
        return d.weekday() < 5  # 0=Mon, 4=Fri

    def _get_rolling_window_trades(self) -> list:
        """获取 5 个交易日滑动窗口内的交易记录"""
        today = date.today()
        # 向前数 5 个交易日 (约 7-9 个日历日)
        trading_days_found = 0
        check_date = today
        while trading_days_found < self.ROLLING_WINDOW_DAYS:
            if self._is_trading_day(check_date):
                trading_days_found += 1
            check_date -= timedelta(days=1)
        # check_date 现在是窗口起始日的前一天
        window_start = check_date + timedelta(days=1)

        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT trade_date, symbol FROM pdt_trades WHERE trade_date >= ?",
                    (window_start.isoformat(),)
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"[PDT] 查询窗口交易失败: {e}")
            return []

    def _get_day_trade_count(self) -> int:
        """获取 5 个交易日滑动窗口内的日内交易次数"""
        return len(self._get_rolling_window_trades())

    def _clean_old_trades(self):
        """清理窗口外的旧交易记录"""
        try:
            trades = self._get_rolling_window_trades()
            if trades:
                oldest_date = min(t[0] for t in trades)
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM pdt_trades WHERE trade_date < ?", (oldest_date,))
                    conn.commit()
        except Exception as e:
            logger.debug(f"[PDT] 清理旧记录失败: {e}")
            
    def _load_state(self) -> PDTState:
        """从 DB 加载状态"""
        state = PDTState()
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT account_equity, is_pdt_account FROM pdt_state WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    state.account_equity = row[0]
                    state.is_pdt_account = bool(row[1])
        except Exception as e:
            logger.error(f"[PDT] 加载状态失败: {e}")
        # day_trade_count 改为实时计算
        state.day_trade_count = self._get_day_trade_count()
        return state

    def update_equity(self, equity: float):
        """更新账户资金"""
        self._state.account_equity = equity
        self._state.is_pdt_account = equity >= self.PDT_EQUITY_THRESHOLD
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE pdt_state SET account_equity = ?, is_pdt_account = ?, last_update = ? WHERE id = 1",
                    (equity, self._state.is_pdt_account, datetime.now().isoformat())
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[PDT] 保存资金状态失败: {e}")

    def record_day_trade(self, symbol: str):
        """记录一次日内交易 (写入 pdt_trades 表)"""
        # 模拟盘不记录 (P0 2026-05-06)
        try:
            from app.core.config_container import get_config
            if get_config().is_simulated:
                return
        except Exception:
            import os
            if os.getenv("TRADE_ENV", "1") == "1":
                return
        today = date.today().isoformat()
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO pdt_trades (trade_date, symbol, timestamp) VALUES (?, ?, ?)",
                    (today, symbol, time.time())
                )
                conn.commit()
            count = self._get_day_trade_count()
            logger.info(f"[PDT] 记录日内交易: {symbol}, 5日窗口内计数: {count}/{self.MAX_DAY_TRADES}")
        except Exception as e:
            logger.error(f"[PDT] 记录日内交易失败: {e}")
        
    def check_trade_permission(self, is_overnight: bool = False) -> Tuple[bool, str, PDTRiskLevel]:
        """
        检查是否允许交易
        
        Args:
            is_overnight: 是否为隔夜仓策略 (非日内交易)
            
        Returns:
            (允许交易, 原因, 风险等级)
        """
        # 模拟盘豁免 PDT 限制 (P0 2026-05-06: 模拟盘无 FINRA 合规风险)
        try:
            from app.core.config_container import get_config
            if get_config().is_simulated:
                return True, "模拟盘豁免 PDT (无 FINRA 合规风险)", PDTRiskLevel.SAFE
        except Exception:
            # 配置未加载时 fallback 到环境变量
            import os
            if os.getenv("TRADE_ENV", "1") == "1":
                return True, "模拟盘豁免 PDT (TRADE_ENV=1)", PDTRiskLevel.SAFE

        # 如果资金 >= $25,000，豁免 PDT 规则
        if self._state.is_pdt_account:
            return True, f"资金充足 (${self._state.account_equity:,.0f} >= $25k)，豁免 PDT", PDTRiskLevel.SAFE
            
        # 如果是隔夜仓，不占用日内交易名额
        if is_overnight:
            return True, "隔夜仓策略，不占用日内名额", PDTRiskLevel.SAFE
            
        # 实时计算 5 个交易日滑动窗口内的交易次数
        count = self._get_day_trade_count()
        
        if count >= self.MAX_DAY_TRADES:
            return False, f"已达日内交易上限 ({self.MAX_DAY_TRADES}次/5交易日)", PDTRiskLevel.LOCKED
        elif count == self.MAX_DAY_TRADES - 1:
            return True, f"今日最后一次日内交易机会 (当前 {count}次)", PDTRiskLevel.WARNING
        else:
            return True, f"日内交易安全 (当前 {count}次)", PDTRiskLevel.SAFE

    def get_status_report(self) -> dict:
        """返回 PDT 状态报告"""
        count = self._get_day_trade_count()
        return {
            "is_pdt_account": self._state.is_pdt_account,
            "account_equity": self._state.account_equity,
            "day_trade_count_5d": count,
            "max_day_trades": self.MAX_DAY_TRADES,
            "risk_level": "SAFE" if count < self.MAX_DAY_TRADES - 1 else ("WARNING" if count == self.MAX_DAY_TRADES - 1 else "LOCKED"),
        }

# 全局单例
_guard: Optional[PDTGuard] = None

def get_pdt_guard(db_path: str = None) -> PDTGuard:
    """获取或创建 PDTGuard 实例"""
    global _guard
    if _guard is None:
        _guard = PDTGuard(db_path)
    return _guard
