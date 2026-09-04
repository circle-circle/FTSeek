#!/usr/bin/env python3
"""
FTSeek-US 每日行情日志存储 (Daily Log Store)

职责：
在盘中运行时，静默记录分钟级行情快照。
这些数据用于盘后的影子测试（Shadow Testing）。

2026-05-13: 初始实现
"""
import sqlite3
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek_us.storage.daily_log")


class DailyLogStore:
    """
    盘后影子测试的数据源。
    记录当日行情的分钟级快照，用于离线重跑策略。
    
    数据库: data/shadow_logs.db
    表名: market_ticks
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else (
            Path(__file__).resolve().parent.parent.parent / "data" / "shadow_logs.db"
        )
        self._ensure_schema()
        self._last_commit = time.time()

    def _ensure_schema(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    close REAL,
                    atr REAL,
                    vix REAL,
                    score REAL,        -- 当时的 IntradayScorer 打分
                    direction TEXT,    -- 当时建议的方向 (LONG/SHORT/NEUTRAL)
                    is_entry REAL,     -- 实盘当时是否入场 (1/0)
                    entry_price REAL,  -- 实盘入场价 (如果入场)
                    exit_price REAL,   -- 实盘平仓价 (如果平仓)
                    is_exit REAL       -- 实盘当时是否平仓 (1/0)
                )
            """)
            conn.commit()
            conn.close()
            logger.info(f"[ShadowLog] 数据库初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"[ShadowLog] Schema 创建失败: {e}")

    def record_tick(self, data: Dict[str, Any]):
        """
        记录一帧行情数据 (被调度循环频繁调用)
        
        Args:
            data: {
                'time': str (ISO),
                'symbol': str,
                'close': float,
                'atr': float,
                'vix': float,
                'score': float,
                'direction': str,
                'is_entry': bool,
                'entry_price': float,
                'is_exit': bool,
                'exit_price': float
            }
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO market_ticks (
                    timestamp, symbol, close, atr, vix, 
                    score, direction, is_entry, entry_price, exit_price, is_exit
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('time'), data.get('symbol'), data.get('close'), 
                data.get('atr'), data.get('vix'), 
                data.get('score'), data.get('direction'), 
                int(data.get('is_entry', False)), data.get('entry_price'), 
                data.get('exit_price'), int(data.get('is_exit', False))
            ))
            
            # DEB-20260903 #28: 每次 INSERT 后立即 commit —
            # 原"10秒延迟commit"优化与"每次新建连接+即关"组合致命:
            # 未 commit 的 INSERT 随 conn.close() 回滚, 绝大部分 tick 静默丢失
            conn.commit()
            self._last_commit = time.time()

            conn.close()
            
        except Exception as e:
            # DEB-20260904 #148: tick 落库失败由 debug 升 warning —
            # 并发锁/IO 失败会持续静默丢复盘数据, debug 级在生产不可见
            logger.warning(f"[ShadowLog] 记录失败 (tick 丢失): {e}")

    def get_day_data(self, symbol: str, date_str: str) -> list:
        """
        获取某天的数据用于回放
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            # 注意：这里假设 timestamp 包含日期信息
            # 如果 timestamp 是完整 ISO 格式，可以用 LIKE
            cursor = conn.execute(
                "SELECT * FROM market_ticks WHERE symbol=? AND timestamp LIKE ? ORDER BY id",
                (symbol, f"{date_str}%")
            )
            rows = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"[ShadowLog] 查询数据失败: {e}")
            return []

    def get_latest_close(self, symbol: str) -> float:
        """获取最新收盘价"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT close FROM market_ticks WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol,)
            )
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 0.0
        except Exception:
            return 0.0
