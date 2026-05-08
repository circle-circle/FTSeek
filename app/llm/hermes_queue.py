"""
Hermes Agent 持久化任务队列 — 封装 hermes_queue.db 的 agent_requests 表。

提供优先级排序的任务轮询、状态流转和完成等待。
结果通过文件协议 (~/.hermes/ftseek/responses/) 返回，DB 仅用于任务追踪。
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.llm.hermes_queue")

DEFAULT_QUEUE_DB = os.path.expanduser("~/.hermes/ftseek/hermes_queue.db")
DEFAULT_RESPONSES_DIR = os.path.expanduser("~/.hermes/ftseek/responses")

# 优先级: 盘前 > 午间 > 盘后 > 其他
PRIORITY_ORDER = {
    "battle_plan": 1,
    "midday_adjust": 2,
    "eod_review": 3,
}


class HermesQueue:
    """持久化任务队列 — 基于 hermes_queue.db"""

    def __init__(self, db_path: str = None, responses_dir: str = None):
        self.db_path = db_path or DEFAULT_QUEUE_DB
        self.responses_dir = responses_dir or DEFAULT_RESPONSES_DIR
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
        self._ensure_table()

    def _ensure_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                request_type TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'us',
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                error_message TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ar_status ON agent_requests(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ar_type ON agent_requests(request_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ar_created ON agent_requests(created_at)"
        )
        conn.commit()
        conn.close()

    def enqueue(
        self,
        request_id: str,
        request_type: str,
        market: str = "hk",
        payload: Dict = None,
    ) -> bool:
        """写入任务到队列"""
        now = datetime.now().isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT OR IGNORE INTO agent_requests
                   (request_id, request_type, market, payload, status, retry_count,
                    max_retries, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, 3, ?, ?)""",
                (request_id, request_type, market, payload_json, now, now),
            )
            conn.commit()
            conn.close()
            logger.info("[HermesQueue] 任务已入队: %s (%s)", request_id, request_type)
            return True
        except Exception as e:
            logger.error("[HermesQueue] 入队失败: %s", e)
            return False

    def poll_pending(self, limit: int = 5) -> List[Dict]:
        """按优先级取 pending 任务"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        priority_case = " ".join([
            "CASE request_type",
            *[f"WHEN '{k}' THEN {v}" for k, v in PRIORITY_ORDER.items()],
            "ELSE 99 END",
        ])
        rows = conn.execute(
            f"""SELECT * FROM agent_requests
                WHERE status = 'pending'
                ORDER BY {priority_case}, created_at ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def mark_processing(self, request_id: str):
        """标记为处理中"""
        self._update(request_id, status="processing")

    def mark_completed(self, request_id: str, result: Dict = None):
        """标记为完成，并将结果写入 response 文件"""
        now = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """UPDATE agent_requests SET status='completed', completed_at=?, updated_at=?
               WHERE request_id=?""",
            (now, now, request_id),
        )
        conn.commit()
        conn.close()

        # 写入 response 文件 (兼容文件协议)
        if result is not None:
            response_file = os.path.join(self.responses_dir, f"{request_id}.json")
            try:
                response = {
                    "request_id": request_id,
                    "timestamp": now,
                    "status": "completed",
                    "result": result,
                }
                with open(response_file, "w", encoding="utf-8") as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("[HermesQueue] 写入 response 文件失败: %s", e)

    def mark_failed(self, request_id: str, error_message: str = ""):
        """标记为失败，递增重试计数"""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat()
        conn.execute(
            """UPDATE agent_requests SET
               status='failed', error_message=?, retry_count=retry_count+1,
               completed_at=?, updated_at=?
               WHERE request_id=?""",
            (error_message, now, now, request_id),
        )
        conn.commit()
        conn.close()
        logger.warning("[HermesQueue] 任务失败: %s — %s", request_id, error_message[:100])

    def requeue_stale(self, max_retries: int = 3) -> int:
        """重新入队失败但未超过最大重试次数的任务"""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """UPDATE agent_requests SET status='pending', updated_at=?
               WHERE status='failed' AND retry_count < ?""",
            (now, max_retries),
        )
        count = cursor.rowcount
        conn.commit()
        conn.close()
        if count:
            logger.info("[HermesQueue] 重新入队 %d 个失败任务", count)
        return count

    def wait_for_completion(self, request_id: str, timeout: int = 120) -> Optional[Dict]:
        """阻塞等待任务完成，返回 result dict 或 None (超时)"""
        start = time.time()
        response_file = os.path.join(self.responses_dir, f"{request_id}.json")

        while time.time() - start < timeout:
            # 优先检查 response 文件
            if os.path.exists(response_file):
                try:
                    with open(response_file, "r", encoding="utf-8") as f:
                        response = json.load(f)
                    return response.get("result", {})
                except Exception as e:
                    logger.warning("[HermesQueue] 读取 response 文件失败: %s", e)

            # 检查 DB 状态
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status FROM agent_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            conn.close()

            if row and row["status"] in ("completed",):
                logger.info("[HermesQueue] 任务已完成 (DB): %s", request_id)
                return {"status": row["status"]}
            if row and row["status"] == "failed":
                logger.warning("[HermesQueue] 任务已失败: %s", request_id)
                return None

            time.sleep(2)

        logger.warning("[HermesQueue] 等待超时: %s (%ds)", request_id, timeout)
        return None

    def _update(self, request_id: str, **kwargs):
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values()) + [now, request_id]
        conn.execute(
            f"UPDATE agent_requests SET {sets}, updated_at=? WHERE request_id=?",
            values,
        )
        conn.commit()
        conn.close()

    def cleanup_completed(self, older_than_hours: int = 24) -> int:
        """清理已完成的任务记录"""
        conn = sqlite3.connect(self.db_path)
        cutoff = datetime.now().isoformat()
        cursor = conn.execute(
            "DELETE FROM agent_requests WHERE status='completed' AND completed_at < ?",
            (cutoff,),
        )
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
