"""
FTSeek-US 轻量级事件日志 (Event Log / WAL)

设计原则:
- 异步追加写，不阻塞交易主路径。
- 记录关键状态变更：信号生成、下单、成交、退出。
- 用于影子盘对比、审计、故障恢复。
"""

import json
import sqlite3
import logging
import threading
import time
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ftseek_us.event_log")

# 线程安全的队列 + 消费者线程，实现异步写入
_queue = []
_lock = threading.Lock()
_db_conn = None
_db_path = str(Path(__file__).parent.parent.parent / "data" / "ftseek_us.db")

def _get_conn():
    global _db_conn
    if _db_conn is None:
        try:
            _db_conn = sqlite3.connect(_db_path, check_same_thread=False)
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            logger.error(f"无法连接 Event Log DB: {e}")
    return _db_conn

def _worker():
    """后台消费者，批量写入数据库"""
    global _db_conn
    while True:
        time.sleep(0.5) # 0.5s 刷新一次
        with _lock:
            batch = _queue[:]
            _queue.clear()
        
        if not batch:
            continue
            
        conn = _get_conn()
        if conn is None:
            continue
            
        try:
            conn.executemany(
                "INSERT INTO event_log (trace_id, event_type, symbol, timestamp, data_json) VALUES (?, ?, ?, ?, ?)",
                batch
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"Event Log 写入失败: {e}")

# 启动后台线程
_worker_thread = threading.Thread(target=_worker, daemon=True)
_worker_thread.start()

def log_event(trace_id: str, event_type: str, symbol: str, data: dict):
    """
    记录事件日志 (异步)
    
    Args:
        trace_id: 追踪 ID
        event_type: 事件类型 (SIGNAL, ORDER, FILL, EXIT)
        symbol: 股票代码
        data: 附加数据字典
    """
    try:
        record = (
            trace_id,
            event_type,
            symbol,
            datetime.now().isoformat(),
            json.dumps(data, ensure_ascii=False)
        )
        with _lock:
            _queue.append(record)
    except Exception as e:
        logger.debug(f"Event Log 入队失败: {e}")
